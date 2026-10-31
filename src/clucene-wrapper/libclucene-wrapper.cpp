#include "libclucene-wrapper.h"
#include "destor.h"
#include "pathstore.h"
#include "TrackingTermScorer.h"
#include "TrackingTermScorer2.h"
#include "berkeley_db_reverse_mapping.h"
#include <boost/dynamic_bitset.hpp>

#include <sys/stat.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <fcntl.h>
#include <unistd.h>

#include <iostream>
#include <algorithm>
#include <random>
#include <chrono>
#include <fstream>
#include <string>
#include <array>
#include <vector>
#include <limits>
#include <unordered_map>
#include <unordered_set>

double searcher_load_time = 0, dtor_time = 0;

static const uint32_t DEFAULT_FILES_PER_SEG = 1024;
static const uint64_t SEG_BITMASK_MAGIC = 0x5345474D41534B32ULL; // "SEGMASK2" (dense)
static const uint64_t SEG_SPARSE_MAGIC  = 0x53454753424C4B31ULL; // "SEGSBLK1" (sparse, blocked)
static const uint64_t SEG_SPARSE_MAGIC_V1 = 0x5345475350415253ULL; // "SEGSPARS" (superseded)
static const uint32_t SEG_BLOCK_CHUNKS = 64;   // chunks per self-contained block

// Path of the bitmask file for a given segment size and representation (encoded
// in the name so several granularities / formats can coexist).
static std::string segBitmaskPath(const std::string& index_path, int files_per_seg, bool sparse) {
    return index_path + (sparse ? "/seg_bitmask_sparse_" : "/seg_bitmask_")
         + std::to_string(files_per_seg) + ".bin";
}

// True iff the two sorted segment-id lists share no element — the two-pointer
// merge Lucene uses to intersect postings.  A skip is safe iff this is true.
static inline bool segListsDisjoint(const uint16_t* a, const uint16_t* ae,
                                    const uint16_t* b, const uint16_t* be) {
    while (a < ae && b < be) {
        if      (*a < *b) ++a;
        else if (*b < *a) ++b;
        else return false;   // common segment -> not disjoint
    }
    return true;
}

const char* chunk_files_unavailable_reason() {
    if (destor.reverse_mapping_type != IN_DOC_REVERSE_MAPPING &&
        destor.reverse_mapping_type != DB_REVERSE_MAPPING)
        return "the reverse mapping must be in-doc or db";
    if (!destor.is_path_indirected)
        return "indirect-path-strings must be yes, so that files have numeric IDs";
    if (destor.reverse_mapping_type == DB_REVERSE_MAPPING &&
        (destor.offsets_mode != NO_OFFSETS || destor.tf_idf))
        return "a db reverse mapping must be built with offsets-mode none and tf-idf no";
    return nullptr;
}

// The IDs of the files that contain a chunk, wherever the index keeps them:
//   in-doc: the chunk's Lucene document stores them as its 'name' values;
//   db:     it stores the chunk fingerprint, under which the Berkeley DB
//           reverse mapping stores them.
// Both list a file once per occurrence of the chunk in it.  Fetch time is
// split between the Lucene document store and the reverse mapping; in both
// modes converting the stored IDs to integers is not timed.
class ChunkFileResolver {
public:
    explicit ChunkFileResolver(const IndexReaderPtr& reader) : reader(reader) {
        if (const char* reason = chunk_files_unavailable_reason()) {
            fprintf(stderr, "cannot read the files of a chunk: %s\n", reason);
            exit(1);
        }
        if (destor.reverse_mapping_type == DB_REVERSE_MAPPING) {
            TIMER_DECLARE(1);
            TIMER_BEGIN(1);
            reverse_mapping.reset(new DbReverseMappingReader());
            TIMER_END(1, open_time);
        }
    }

    // Appends the IDs of the files that contain chunk docId to out.
    void fetch(int32_t docId, std::vector<int32_t>& out) {
        static const std::wstring nameField = toWstring("name");
        TIMER_DECLARE(1);
        TIMER_BEGIN(1);
        DocumentPtr document = reader->document(docId);
        if (reverse_mapping) {
            std::string fp = StringUtils::toUTF8(document->get(nameField));
            TIMER_END(1, doc_store_time);
            reverse_mapping->get_file_ids(fp, out);
        } else {
            const auto& fileIDs = document->getValues(nameField);
            TIMER_END(1, doc_store_time);
            out.reserve(out.size() + fileIDs.size());
            for (const auto& fileID : fileIDs)
                out.push_back(std::stoi(fileID));
        }
    }

    // Microseconds; all zero for in-doc except the document store.
    double reverse_mapping_time() const { return reverse_mapping ? reverse_mapping->read_time : 0; }
    double doc_store_time = 0;
    double open_time = 0;              // opening the reverse mapping

private:
    IndexReaderPtr reader;
    std::unique_ptr<DbReverseMappingReader> reverse_mapping;
};

// Every chunk's file IDs in CSR form: chunk d's are
// flatFiles[offsets[d] .. offsets[d + 1]).  Deleted chunks have none.
struct ChunkFiles {
    std::vector<uint32_t> flatFiles;
    std::vector<uint64_t> offsets;
    uint32_t maxFid = 0;
    int32_t  maxDoc = 0;
};

// One pass over the chunks of the index at index_path, decoding each chunk's
// file list once.  For a db index every chunk is a reverse-mapping lookup.
static ChunkFiles readChunkFiles(const std::string& index_path) {
    auto reader = IndexReader::open(FSDirectory::open(toWstring(index_path)));
    ChunkFiles cf;
    cf.maxDoc = reader->maxDoc();
    cf.offsets.assign((size_t)cf.maxDoc + 1, 0);
    cf.flatFiles.reserve((size_t)cf.maxDoc * 4);
    {
        ChunkFileResolver resolver(reader);
        std::vector<int32_t> fileIDs;
        for (int32_t docId = 0; docId < cf.maxDoc; ++docId) {
            cf.offsets[docId] = cf.flatFiles.size();
            if (reader->isDeleted(docId)) continue;
            fileIDs.clear();
            resolver.fetch(docId, fileIDs);
            for (size_t i = 0; i < fileIDs.size(); ++i) {
                uint32_t f = (uint32_t)fileIDs[i];
                cf.flatFiles.push_back(f);
                if (f > cf.maxFid) cf.maxFid = f;
            }
        }
    }
    cf.offsets[cf.maxDoc] = cf.flatFiles.size();
    reader->close();
    return cf;
}

// Build and persist a per-chunk segment bitmask at a chosen granularity.
//
// A segment is a block of `files_per_seg` consecutive file IDs; a chunk's mask
// has bit s set iff some file in segment s contains that chunk.  With finer
// segments the number of segments exceeds 64, so each chunk's mask spans
// `words` 64-bit words (words = ceil(numSegments/64)).  The file layout is:
//     [magic][files_per_seg][words][num_chunks]  then num_chunks * words uint64.
//
// Built offline (once per size) so queries never touch the reverse mapping.  A
// single pass decodes each chunk's file list into a flat CSR buffer — which also
// yields the max file ID (hence the word count) — then bits are set in memory.
void saveSegmentBitmask(const std::string& index_path, int files_per_seg) {
    if (files_per_seg <= 0) files_per_seg = DEFAULT_FILES_PER_SEG;

    // Pass 1 (decode once): gather per-chunk file IDs into CSR form + max file ID.
    const ChunkFiles cf = readChunkFiles(index_path);
    const std::vector<uint32_t>& flatFiles = cf.flatFiles;
    const std::vector<uint64_t>& offsets = cf.offsets;
    const int32_t maxDoc = cf.maxDoc;
    const uint32_t maxFid = cf.maxFid;

    int numSegments = (int)(maxFid / (uint32_t)files_per_seg) + 1;
    int words = (numSegments + 63) / 64;

    // Pass 2 (in memory): set each chunk's segment bits across `words` words.
    std::vector<uint64_t> out((size_t)maxDoc * words, 0ULL);
    for (int32_t docId = 0; docId < maxDoc; ++docId) {
        uint64_t* row = &out[(size_t)docId * words];
        for (uint64_t k = offsets[docId]; k < offsets[docId + 1]; ++k) {
            uint32_t seg = flatFiles[k] / (uint32_t)files_per_seg;
            row[seg >> 6] |= (1ULL << (seg & 63));
        }
    }

    std::string path = segBitmaskPath(index_path, files_per_seg, false);
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) {
        fprintf(stderr, "saveSegmentBitmask: cannot write %s\n", path.c_str());
        return;
    }
    uint64_t header[4] = { SEG_BITMASK_MAGIC, (uint64_t)files_per_seg,
                           (uint64_t)words, (uint64_t)maxDoc };
    fwrite(header, sizeof(uint64_t), 4, f);
    fwrite(out.data(), sizeof(uint64_t), out.size(), f);
    fclose(f);
    printf("Segment bitmask saved: fps=%d, segments=%d, words/chunk=%d, %d chunks, %.1f MB -> %s\n",
           files_per_seg, numSegments, words, maxDoc,
           out.size() * 8.0 / (1 << 20), path.c_str());
}

// Build and persist the SPARSE variant: per chunk, the sorted list of segment
// ids it touches.  Chunks are grouped into self-contained blocks of
// SEG_BLOCK_CHUNKS, each holding its own uint16 local offsets followed by its
// own segment ids:
//     [magic][files_per_seg][num_chunks N][block_chunks B]
//     [num_blocks NB][total_entries E][blocks_bytes][reserved]
//     block_offsets : (NB+1) x uint64   byte offset of each block
//     blocks        : per block { uint16 local_off[B+1]; uint16 segs[...] }
//
// The earlier layout kept one global uint32 offset array followed by all the
// segment ids, so every lookup touched two regions tens of MB apart and the
// kernel saw two interleaved streams instead of one.  Here the only wide jump
// is into block_offsets, which is ~8 bytes per 64 chunks (a couple of MB for a
// 15M-chunk index) and stays resident, while the bulk is read as a single
// forward stream.  Local uint16 offsets also cost less than global uint32 ones.
void saveSegmentBitmaskSparse(const std::string& index_path, int files_per_seg) {
    if (files_per_seg <= 0) files_per_seg = DEFAULT_FILES_PER_SEG;

    // Pass 1 (decode once): per-chunk file IDs into CSR form + max file ID.
    const ChunkFiles cf = readChunkFiles(index_path);
    const std::vector<uint32_t>& flatFiles = cf.flatFiles;
    const std::vector<uint64_t>& offsets = cf.offsets;
    const int32_t maxDoc = cf.maxDoc;
    const uint32_t maxFid = cf.maxFid;

    int numSegments = (int)(maxFid / (uint32_t)files_per_seg) + 1;
    if (numSegments > 65535) {
        fprintf(stderr, "saveSegmentBitmaskSparse: %d segments exceeds uint16 range; "
                        "use a larger files-per-seg or widen the id type.\n", numSegments);
        return;
    }

    const uint32_t B  = SEG_BLOCK_CHUNKS;
    const uint64_t NB = ((uint64_t)maxDoc + B - 1) / B;

    // Pass 2 (in memory): build each block, recording its byte offset.
    std::vector<uint64_t> blockOffsets((size_t)NB + 1, 0);
    std::vector<uint8_t>  blocks;
    blocks.reserve((size_t)flatFiles.size() * 2 + (size_t)NB * (B + 1) * 2);
    std::vector<uint16_t> local(B + 1), tmp;
    uint64_t totalEntries = 0;

    for (uint64_t b = 0; b < NB; ++b) {
        blockOffsets[(size_t)b] = blocks.size();
        std::vector<uint16_t> blockSegs;
        uint32_t entries = 0;
        for (uint32_t i = 0; i < B; ++i) {
            local[i] = (uint16_t)entries;
            int64_t docId = (int64_t)b * B + i;
            if (docId >= maxDoc) continue;          // tail block: pad with equal offsets
            tmp.clear();
            for (uint64_t k = offsets[(size_t)docId]; k < offsets[(size_t)docId + 1]; ++k)
                tmp.push_back((uint16_t)(flatFiles[k] / (uint32_t)files_per_seg));
            std::sort(tmp.begin(), tmp.end());
            tmp.erase(std::unique(tmp.begin(), tmp.end()), tmp.end());
            blockSegs.insert(blockSegs.end(), tmp.begin(), tmp.end());
            entries += (uint32_t)tmp.size();
        }
        if (entries > 65535) {   // the local offsets are uint16
            fprintf(stderr, "saveSegmentBitmaskSparse: block %llu holds %u segment ids, more than "
                            "its uint16 offsets address; use a larger files-per-seg.\n",
                    (unsigned long long)b, entries);
            return;
        }
        local[B] = (uint16_t)entries;
        totalEntries += entries;

        const uint8_t* lo = reinterpret_cast<const uint8_t*>(local.data());
        blocks.insert(blocks.end(), lo, lo + (size_t)(B + 1) * sizeof(uint16_t));
        const uint8_t* sg = reinterpret_cast<const uint8_t*>(blockSegs.data());
        blocks.insert(blocks.end(), sg, sg + blockSegs.size() * sizeof(uint16_t));
        while (blocks.size() % 8) blocks.push_back(0);   // keep blocks 8-byte aligned
    }
    blockOffsets[(size_t)NB] = blocks.size();

    std::string path = segBitmaskPath(index_path, files_per_seg, true);
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) {
        fprintf(stderr, "saveSegmentBitmaskSparse: cannot write %s\n", path.c_str());
        return;
    }
    uint64_t header[8] = { SEG_SPARSE_MAGIC, (uint64_t)files_per_seg, (uint64_t)maxDoc,
                           (uint64_t)B, NB, totalEntries, (uint64_t)blocks.size(), 0 };
    fwrite(header, sizeof(uint64_t), 8, f);
    fwrite(blockOffsets.data(), sizeof(uint64_t), blockOffsets.size(), f);
    fwrite(blocks.data(), 1, blocks.size(), f);
    fclose(f);
    double mb = (64 + blockOffsets.size() * 8.0 + blocks.size()) / (1 << 20);
    printf("Segment bitmask (sparse) saved: fps=%d, segments=%d, %d chunks, %llu blocks, "
           "%llu entries (%.2f seg/chunk), %.1f MB -> %s\n",
           files_per_seg, numSegments, maxDoc, (unsigned long long)NB,
           (unsigned long long)totalEntries, (double)totalEntries / maxDoc, mb, path.c_str());
}

// Read-only, mmap-backed view of a per-chunk segment index in either format:
//   dense  : `words` 64-bit words per chunk; row(c) returns them.
//   sparse : CSR of sorted segment ids; segBegin(c)/segEnd(c) delimit c's list.
struct SegBitmask {
    bool            sparse = false;
    size_t          count = 0;        // number of chunks
    int             files_per_seg = DEFAULT_FILES_PER_SEG;
    // dense
    const uint64_t* data = nullptr;   // first chunk's words
    int             words = 1;        // 64-bit words per chunk
    // sparse (blocked): small hot offset table + one streaming region of blocks
    const uint64_t* blockOffs = nullptr;   // (num_blocks+1) byte offsets into blocks
    const uint8_t*  blocks    = nullptr;
    uint32_t        blockChunks = SEG_BLOCK_CHUNKS;
    size_t size()  const { return count; }
    bool   empty() const { return count == 0; }
    const uint64_t* row(size_t c) const { return data + c * (size_t)words; }
    // Locate chunk c's block once; both ends come from the same cache line.
    const uint16_t* blockLocal(size_t c) const {
        return reinterpret_cast<const uint16_t*>(blocks + blockOffs[c / blockChunks]);
    }
    const uint16_t* segBegin(size_t c) const {
        const uint16_t* lo = blockLocal(c);
        return lo + (blockChunks + 1) + lo[c % blockChunks];
    }
    const uint16_t* segEnd(size_t c) const {
        const uint16_t* lo = blockLocal(c);
        return lo + (blockChunks + 1) + lo[(c % blockChunks) + 1];
    }
};

// Map the pre-built segment index (dense or sparse) into memory, once per
// (index, files_per_seg, format) per process.  mmap avoids copying the file;
// pages fault in only for the chunk IDs actually accessed and stay resident in
// the page cache across process invocations, so repeated queries pay ~0 to load.
static const SegBitmask& loadSegmentBitmask(const std::string& index_path,
                                            int files_per_seg, bool sparse) {
    if (files_per_seg <= 0) files_per_seg = DEFAULT_FILES_PER_SEG;
    static std::unordered_map<std::string, SegBitmask> cache;
    std::string path = segBitmaskPath(index_path, files_per_seg, sparse);
    auto it = cache.find(path);
    if (it != cache.end()) return it->second;   // already mapped in this process

    SegBitmask sb;
    int fd = open(path.c_str(), O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "loadSegmentBitmask: %s not found — run 'destor -o -P %d%s' first\n",
                path.c_str(), files_per_seg, sparse ? " -S" : "");
        return cache.emplace(path, sb).first->second;
    }
    struct stat st;
    if (fstat(fd, &st) == 0 && st.st_size > (off_t)(4 * sizeof(uint64_t))) {
        void* addr = mmap(nullptr, (size_t)st.st_size, PROT_READ, MAP_SHARED, fd, 0);
        if (addr != MAP_FAILED) {
            // Both phases walk chunk ids in ascending order, so the mapping is
            // read as a forward stream.  Say so: without the hint the kernel
            // sizes its readahead window from the fault pattern alone, and a
            // format that interleaves two distant regions looks random to it.
            madvise(addr, (size_t)st.st_size, MADV_SEQUENTIAL);
            // Lazily mapping the file means its I/O is paid by whichever phase
            // first touches a page -- in practice the AND phase, whose reported
            // time then mixes intersection work with bitmask reads and is not
            // comparable between formats of different size.  When asked, fault
            // the whole mapping in here instead, so the cost lands in the seg
            // load timer where it can be seen.  Off by default: a query that
            // touches few chunks should not read the whole file.
            if (destor.seg_prefault) {
                madvise(addr, (size_t)st.st_size, MADV_WILLNEED);
                volatile uint8_t sink = 0;
                const uint8_t* p = reinterpret_cast<const uint8_t*>(addr);
                for (off_t off = 0; off < st.st_size; off += 4096) sink ^= p[off];
                (void)sink;
            }
            const uint64_t* h = reinterpret_cast<const uint64_t*>(addr);
            if (h[0] == SEG_BITMASK_MAGIC) {
                sb.sparse        = false;
                sb.files_per_seg = (int)h[1];
                sb.words         = (int)h[2];
                sb.count         = (size_t)h[3];
                sb.data          = h + 4;
            } else if (h[0] == SEG_SPARSE_MAGIC) {
                sb.sparse       = true;
                sb.files_per_seg = (int)h[1];
                sb.count        = (size_t)h[2];
                sb.blockChunks  = (uint32_t)h[3];
                uint64_t numBlocks = h[4];
                sb.blockOffs    = h + 8;
                sb.blocks       = reinterpret_cast<const uint8_t*>(sb.blockOffs + (numBlocks + 1));
            } else if (h[0] == SEG_SPARSE_MAGIC_V1) {
                fprintf(stderr, "loadSegmentBitmask: %s uses the superseded two-region sparse "
                                "layout; rebuild it with 'destor -o -P %d -S'\n",
                        path.c_str(), files_per_seg);
            } else {
                fprintf(stderr, "loadSegmentBitmask: bad magic in %s\n", path.c_str());
            }
        } else {
            fprintf(stderr, "loadSegmentBitmask: mmap failed for %s\n", path.c_str());
        }
    }
    close(fd);   // mapping stays valid after the fd is closed
    return cache.emplace(path, sb).first->second;
}

extern int job;

std::wstring toWstring(const std::string& str) {
	std::wstring temp(str.size(), L'\0');
	for (size_t i = 0; i < str.size(); i++)
	{
		if (0 <= str[i] && str[i] <= 127) {
			temp[i] = wchar_t(str[i]);
		} else {
			temp[i] = L'?';
		}
	}
	return temp;
}

const wchar_t* _STOP_LIST[] = {
    L"a", L"an", L"and", L"are", L"as", L"at", L"be", L"but", L"by",
    L"for", L"if", L"in", L"into", L"is", L"it", L"no", L"not", L"of",
    L"on", L"or", L"such", L"that", L"the", L"their", L"then", L"there",
    L"these", L"they", L"this", L"to", L"was", L"will", L"with",L"auto", 
	L"break", L"case", L"char", L"const", L"continue", L"default", L"do", 
	L"double", L"else", L"enum", L"extern", L"float", L"for", L"go", L"to", 
	L"if", L"int", L"long", L"register", L"return", L"short", L"signed", 
	L"sizeof", L"static", L"struct", L"switch", L"typedef", L"union", 
	L"unsigned", L"void", L"volatile", L"while", L"b", L"c", L"d",
	L"e", L"f", L"g", L"h", L"i", L"j", L"k", L"l", L"m", L"n", L"o", L"p",
	L"q", L"r", L"s", L"t", L"u", L"v", L"w", L"x", L"y", L"z", L"0", L"1", 
	L"2", L"3", L"4", L"6", L"7", L"8", L"9", L"10"
};

HashSet<String> STOP_LIST;

class MyHitCollector : public Collector {
public:
	IndexReaderPtr m_reader;
	std::vector<std::string> m_results;
	std::vector<int32_t> m_docIds;
	std::vector<double> m_scores;
	std::vector<uint32_t> m_termFreqs;
	int32_t m_docBase;
	SyncQueue* m_queue;
	ScorerPtr m_scorer;

	explicit MyHitCollector(SyncQueue* queue) : m_queue(queue) {}
	explicit MyHitCollector() : MyHitCollector(nullptr) {}

    virtual void setNextReader(const IndexReaderPtr& reader, int32_t docBase) override {
         this->m_docBase = docBase;
		 this->m_reader = reader;
		 this->m_scorer = nullptr;
	}

	virtual bool acceptsDocsOutOfOrder() override {
		return true;
	}

	virtual void setScorer(const ScorerPtr& scorer) override {
		m_scorer = scorer;
		m_scorer->setDocBase(m_docBase);
	}

	virtual void collect(const int32_t doc) override {
		if (destor.is_threaded_lookup) {
			DocumentPtr document  = m_reader->document(doc);
			auto filename(document->get(toWstring("name")));
			std::string filenameUtf8 = StringUtils::toUTF8(filename);
			m_results.push_back(filenameUtf8);
			sync_queue_push(m_queue, new std::string(filenameUtf8));
		} else {
			m_docIds.push_back(m_docBase + doc);
			if (m_scorer && destor.tf_idf) {
				if (job == DESTOR_DEDUPLICATED_LOOKUP) {
					m_termFreqs.push_back(m_scorer->termFreq());
				} else {
					m_scores.push_back(m_scorer->score());
				}
			}
		}
	}

	virtual std::vector<std::string> getResultsFromSearcher(IndexSearcherPtr searcher) {
		TIMER_DECLARE(7);
		double total_docs_fetch_files_time = 0;
		std::vector<std::string> results;
		std::cout << "Number of chunks: "<< m_docIds.size() << std::endl;
		TIMER_BEGIN(7);
		for (auto doc : m_docIds) {
			DocumentPtr document  = searcher->doc(doc);
			std::cout << "doc: " << doc << std::endl;
			for (const auto& filename : document->getValues(toWstring("name"))) {
				std::cout << "file: " << StringUtils::toUTF8(filename) << std::endl;
				results.push_back(StringUtils::toUTF8(filename));
			}
		}
		TIMER_END(7, total_docs_fetch_files_time);
		printf("fetch files time: %.3f\n", total_docs_fetch_files_time / 1000000);
		std::cout << "Number of files before filtering: "<< results.size() << std::endl;
		return results;
	}

	virtual std::vector<std::string> getResultsFromSearcherWithTrackingBitmap(
		IndexSearcherPtr searcher,
		std::unordered_map<int32_t, boost::dynamic_bitset<>>* docBitmaps,
		size_t num_keywords) {

		TIMER_DECLARE(2);
		TIMER_DECLARE(6);
		TIMER_DECLARE(7);
		double total_accessing_docs_in_bitmap_time = 0, total_docs_file_marking_time = 0, file_coverage_check_time = 0;
		std::vector<std::string> results;
		std::unordered_map<int32_t, boost::dynamic_bitset<>> fileBitmaps;

		std::cout << "Number of chunks: " << m_docIds.size() << ", " << docBitmaps->size() << std::endl;

		ChunkFileResolver chunkFiles(searcher->getIndexReader());
		std::vector<int32_t> fileIDs;

		for (auto doc : m_docIds) {
			TIMER_BEGIN(7);
			auto it = docBitmaps->find(doc);
			if (it == docBitmaps->end()) {
				std::cerr << "Warning: docBitmaps missing entry for doc " << doc << std::endl;		
				continue;
			}
			const auto& chunkBitmap = it->second;
			TIMER_END(7, total_accessing_docs_in_bitmap_time);

			fileIDs.clear();
			chunkFiles.fetch(doc, fileIDs);
			std::cout << "doc: " << doc << std::endl;
			TIMER_BEGIN(2);
			for (int file_id : fileIDs) {
				std::cout << "file: " << file_id << std::endl;

				// Create or get existing bitmap for this file
				auto& fileBitmap = fileBitmaps.emplace(file_id, chunkBitmap.size()).first->second;

				// Resize if needed (only if chunkBitmap is larger than fileBitmap), not supposed to happen
				if (fileBitmap.size() < chunkBitmap.size())
					fileBitmap.resize(chunkBitmap.size());

				// Efficient OR using boost::dynamic_bitset operator
				fileBitmap |= chunkBitmap;
			}
			TIMER_END(2, total_docs_file_marking_time);
		}

		std::cout << "Number of files before filtering: " << fileBitmaps.size() << std::endl;

		// Check which files have all bits true
		TIMER_BEGIN(6);
		for (const auto& [filename, bitmap] : fileBitmaps) {
    	// Only consider first num_keywords bits
			if (bitmap.count() == num_keywords) {
					results.push_back(std::to_string(filename));
			}
		}
		TIMER_END(6, file_coverage_check_time);

		printf("accessing docs in bitmap time: %.3f\n", total_accessing_docs_in_bitmap_time / 1000000);
		printf("fetch files time: %.3f\n", (chunkFiles.doc_store_time + chunkFiles.reverse_mapping_time()) / 1000000);
		printf("reverse mapping time: %.3f\n", chunkFiles.reverse_mapping_time() / 1000000);
		printf("reverse mapping open time: %.3f\n", chunkFiles.open_time / 1000000);
		printf("file marking time: %.3f\n", total_docs_file_marking_time / 1000000);
		printf("file coverage check time: %.3f\n", file_coverage_check_time / 1000000);

		return results;
	}

	// Cache: chunkID -> vector<int32_t> fileIds
	using ChunkToFilesMap = std::unordered_map<int32_t, std::vector<int32_t>>;
	struct SegmentedFileCounts {
		static const uint32_t SEG_SHIFT = 12;                  // segment = 4096 entries
		static const uint32_t SEG_SIZE  = 1u << SEG_SHIFT;
		static const uint32_t SEG_MASK  = SEG_SIZE - 1;
		uint16_t num_resizes;

		// segments[i] points to an array<uint16_t, SEG_SIZE> (heap-allocated)
		std::vector<uint16_t*> segments;  // nullptr means "no segment yet"

		~SegmentedFileCounts() {
			for (auto* seg : segments) {
				free(seg);
			}
		}

		// First term: get or create
		uint16_t& getOrCreate(int32_t fileId) {
			uint32_t id = static_cast<uint32_t>(fileId);
			uint32_t segIdx = id >> SEG_SHIFT;
			uint32_t offset = id & SEG_MASK;

			if (segIdx >= segments.size()) {
				segments.resize(segIdx + 1, nullptr);
			}
			uint16_t*& seg = segments[segIdx];
			if (!seg) {
				// zero-initialized
				seg = (uint16_t*)calloc(SEG_SIZE, sizeof(uint16_t));
			}
			return seg[offset];
		}

		// Later terms: only if exists
		uint16_t* getIfExists(int32_t fileId) {
			uint32_t id = static_cast<uint32_t>(fileId);
			uint32_t segIdx = id >> SEG_SHIFT;
			if (segIdx >= segments.size()) return nullptr;
			uint16_t* seg = segments[segIdx];
			if (!seg) return nullptr;
			uint32_t offset = id & SEG_MASK;
			return &seg[offset];
		}

		// Collect results: all fileIds with count == requiredCount
		void collectResults(uint16_t requiredCount,
							std::vector<std::string>& results) const {
			for (uint32_t segIdx = 0; segIdx < segments.size(); ++segIdx) {
				uint16_t* seg = segments[segIdx];
				if (!seg) continue;
				uint32_t baseId = segIdx << SEG_SHIFT;
				for (uint32_t offset = 0; offset < SEG_SIZE; ++offset) {
					if (seg[offset] == requiredCount) {
						int32_t fid = static_cast<int32_t>(baseId + offset);
						results.push_back(std::to_string(fid));
					}
				}
			}
		}

		void collectResultsInt32(uint16_t requiredCount,
                         std::vector<int32_t>& out) const {
			for (uint32_t segIdx = 0; segIdx < segments.size(); ++segIdx) {
				uint16_t* seg = segments[segIdx];
				if (!seg) continue;
				uint32_t baseId = segIdx << SEG_SHIFT;
				for (uint32_t offset = 0; offset < SEG_SIZE; ++offset) {
					if (seg[offset] == requiredCount) {
						out.push_back((int32_t)(baseId + offset));
					}
				}
			}
		}

		// Rough memory usage estimate
		size_t memoryUsageBytes() const {
			size_t bytes = sizeof(segments) + segments.capacity() * sizeof(uint16_t*);
			for (auto* seg : segments) {
				if (seg) {
					bytes += SEG_SIZE * sizeof(uint16_t);
				}
			}
			return bytes;
		}
	};

	// Size-estimation helpers
	inline size_t estimateChunkToFilesBytes(const ChunkToFilesMap& chunkToFiles) {
		size_t bytes =
			sizeof(chunkToFiles) +
			chunkToFiles.bucket_count() * sizeof(void*);

		for (const auto& p : chunkToFiles) {
			// key
			bytes += sizeof(p.first); // int32_t chunkID

			// value: std::vector<int32_t>
			const auto& vec = p.second;
			bytes += sizeof(vec) + vec.capacity() * sizeof(int32_t);

			// approximate node overhead
			bytes += sizeof(void*) * 2;
		}
		return bytes;
	}

	virtual std::vector<std::string> getResultsFromSearcherWithTrackingVector(
		IndexSearcherPtr searcher,
		std::vector<std::vector<int32_t>>* matchTracker)
	{
		std::vector<int32_t> all_terms((int32_t)matchTracker->size());
		for (int32_t i = 0; i < (int32_t)matchTracker->size(); ++i) all_terms[i] = i;
		return getResultsFromSearcherInclExcl(searcher, matchTracker, all_terms, {});
	}

	virtual std::vector<std::string> getResultsFromSearcherInclExcl(
    IndexSearcherPtr searcher,
    std::vector<std::vector<int32_t>>* matchTracker,
    std::vector<int32_t> included_terms,
    std::vector<int32_t> excluded_terms,
    const SegBitmask* seg_bitmask = nullptr)
	{
		TIMER_DECLARE(1);
		TIMER_DECLARE(2);
		TIMER_DECLARE(4);

		double total_sorting_keywords_time = 0;
		double total_and_time              = 0;   // count-advancing AND intersection
		double total_not_time              = 0;   // NOT filter + result gathering

		std::vector<std::string> results;
		bool no_common_files = false;

		uint32_t num_files_fetched  = 0;
		uint32_t num_chunks_fetched = 0;
		size_t   seg_skips_and      = 0;   // included chunks skipped during AND
		size_t   seg_skips_not      = 0;   // excluded chunks skipped during NOT
		size_t   excl_chunks_total  = 0;   // total excluded chunks (skip denominator)

		// Major faults per phase.  With a lazily mapped segment index the disk
		// reads show up as time inside whichever phase touches the page, not as
		// load time, so report the faults alongside the timings to make clear
		// how much of a phase was I/O rather than work.
		auto majflt = []() -> long {
			struct rusage ru;
			getrusage(RUSAGE_SELF, &ru);
			return ru.ru_majflt;
		};
		long majflt_and = 0, majflt_not = 0;

		// --- matchTracker size accounting ---
		size_t matchTracker_total_bytes = sizeof(*matchTracker);
		matchTracker_total_bytes += matchTracker->capacity() * sizeof(std::vector<int32_t>);
		for (const auto& inner : *matchTracker) {
			matchTracker_total_bytes += inner.capacity() * sizeof(int32_t);
		}

		// --- Sort INCLUDED terms only by #chunks ---
		TIMER_BEGIN(1);
		std::vector<int32_t> sorted_termIDs_by_matches = included_terms;
		std::sort(sorted_termIDs_by_matches.begin(),
				sorted_termIDs_by_matches.end(),
				[matchTracker](int32_t t1, int32_t t2) {
					return (*matchTracker)[t1].size() < (*matchTracker)[t2].size();
				});
		TIMER_END(1, total_sorting_keywords_time);

		// Included-chunk accounting: total posting size across included terms, and
		// the AND-skip denominator (chunks in every term after the seed term 0,
		// which are the ones actually subject to the AND-phase skip test).
		size_t included_chunks_total = 0, and_eligible_chunks = 0;
		for (size_t i = 0; i < sorted_termIDs_by_matches.size(); ++i) {
			size_t n = (*matchTracker)[sorted_termIDs_by_matches[i]].size();
			included_chunks_total += n;
			if (i > 0) and_eligible_chunks += n;
		}

		// --- Caches / state ---
		ChunkToFilesMap chunkToFiles;   // chunkID -> vector<int32_t> fileIds
		SegmentedFileCounts fileState;  // fileId  -> seenCount
		ChunkFileResolver chunkFiles(searcher->getIndexReader());

		// Helper: fetch and cache file IDs for a chunk
		auto get_files_for_chunk = [&](int32_t chunkID) -> const std::vector<int32_t>& {
			auto it = chunkToFiles.find(chunkID);
			if (it != chunkToFiles.end()) {
				return it->second; // cache hit
			}

			std::vector<int32_t> files;
			chunkFiles.fetch(chunkID, files);

			++num_chunks_fetched;
			num_files_fetched += (uint32_t)files.size();

			auto inserted = chunkToFiles.insert(std::make_pair(chunkID, std::move(files)));
			return inserted.first->second;
		};

		// --- Segment-filter working state (dense multi-word OR sparse lists) ---
		const bool segFilter = (seg_bitmask != nullptr);
		const bool segSparse = segFilter && seg_bitmask->sparse;
		const int  segW      = (segFilter && !segSparse) ? seg_bitmask->words : 0;
		const int  segFPS    = segFilter ? seg_bitmask->files_per_seg
		                                 : (int)DEFAULT_FILES_PER_SEG;
		// Current candidate segment-set (dense mask or sorted id-list), plus a
		// per-term accumulator of the segments of files that (re)entered the set.
		std::vector<uint64_t> cand_mask, acc_mask;   // dense
		std::vector<uint16_t> cand_segs, acc_raw;    // sparse (cand sorted-unique)
		auto accSeg = [&](int32_t fid) {
			uint32_t seg = (uint32_t)fid / (uint32_t)segFPS;
			if (segSparse) acc_raw.push_back((uint16_t)seg);
			else           acc_mask[seg >> 6] |= (1ULL << (seg & 63));
		};
		auto resetAcc = [&]() {
			if (segSparse) acc_raw.clear();
			else           acc_mask.assign(segW, 0ULL);
		};
		auto finalizeCandidate = [&]() {   // candidate := accumulator
			if (segSparse) {
				std::sort(acc_raw.begin(), acc_raw.end());
				acc_raw.erase(std::unique(acc_raw.begin(), acc_raw.end()), acc_raw.end());
				cand_segs.swap(acc_raw);
			} else {
				cand_mask.swap(acc_mask);
			}
		};
		// true iff chunkID's segments do not overlap the current candidate => skip.
		auto segDisjoint = [&](int32_t chunkID) -> bool {
			if (chunkID < 0 || (size_t)chunkID >= seg_bitmask->size()) return false;
			if (segSparse)
				return segListsDisjoint(seg_bitmask->segBegin(chunkID), seg_bitmask->segEnd(chunkID),
				                        cand_segs.data(), cand_segs.data() + cand_segs.size());
			const uint64_t* r = seg_bitmask->row(chunkID);
			for (int w = 0; w < segW; ++w) if (r[w] & cand_mask[w]) return false;
			return true;
		};

		// --- AND (Count-Advancing) over INCLUDED terms ---
		majflt_and = majflt();
		TIMER_BEGIN(2);

		const int32_t numIncluded = (int32_t)sorted_termIDs_by_matches.size();
		if (numIncluded == 0) {
			// Pure NOT is undefined without a universe of all files.
			TIMER_END(2, total_and_time);
			return results;
		}

		for (int32_t termIndex = 0; termIndex < numIncluded; ++termIndex) {
			int32_t termId = sorted_termIDs_by_matches[termIndex];
			size_t advancedThisTerm = 0;
			if (segFilter) resetAcc();   // accumulate this term's candidate segments

			const auto& chunksForTerm = (*matchTracker)[termId];
			for (size_t ci = 0; ci < chunksForTerm.size(); ++ci) {
				int32_t chunkID = chunksForTerm[ci];

				if (termIndex == 0) {
					// Seed the intersection from the smallest term and record
					// the segments its files span.
					const std::vector<int32_t>& files = get_files_for_chunk(chunkID);
					for (size_t fi = 0; fi < files.size(); ++fi) {
						int32_t fid = files[fi];
						uint16_t& cnt = fileState.getOrCreate(fid);
						if (cnt == 0) {
							cnt = 1;
							if (segFilter) accSeg(fid);
						}
					}
				} else {
					// Early exit: if none of this chunk's segments overlap the
					// current intersection, no file of it can be a candidate,
					// so skip it without fetching its file list.
					if (segFilter && segDisjoint(chunkID)) {
						++seg_skips_and;
						continue;
					}
					const std::vector<int32_t>& files = get_files_for_chunk(chunkID);
					for (size_t fi = 0; fi < files.size(); ++fi) {
						int32_t fid = files[fi];
						uint16_t* cntPtr = fileState.getIfExists(fid);
						if (!cntPtr) continue;
						if (*cntPtr == (uint16_t)termIndex) {
							*cntPtr = (uint16_t)(termIndex + 1);
							++advancedThisTerm;
							if (segFilter) accSeg(fid);
						}
					}
				}
			}

			// The intersection just shrank to the files that (re)entered it;
			// tighten the candidate to their segments for the next term / NOT.
			if (segFilter) finalizeCandidate();
			if (termIndex > 0 && advancedThisTerm == 0) {
				no_common_files = true;
				break;
			}
		}

		// If AND already empty -> done
		if (no_common_files) {
			TIMER_END(2, total_and_time);
			return results;
		}

		// --- Collect AND survivors as int32 candidates ---
		std::vector<int32_t> candidates;
		candidates.reserve(1024); // optional
		fileState.collectResultsInt32((uint16_t)numIncluded, candidates);

		TIMER_END(2, total_and_time);   // AND (intersection) phase ends here
		majflt_and = majflt() - majflt_and;

		if (candidates.empty()) {
			return results;             // nothing survived AND; NOT phase not run
		}

		// --- NOT filter: kill any AND-survivor that appears in excluded terms ---
		majflt_not = majflt();
		TIMER_BEGIN(4);
		size_t alive = candidates.size();

		// candidate_mask already reflects the segments of the AND survivors,
		// having been narrowed alongside the intersection above. An excluded
		// chunk can be skipped entirely if none of its segments overlap it.
		// The popcount is the segment locality of the survivors: the fewer
		// segments they span, the more excluded chunks the filter can skip.
		int candidate_segments = -1;
		if (segFilter) {
			if (segSparse) candidate_segments = (int)cand_segs.size();
			else {
				candidate_segments = 0;
				for (int w = 0; w < segW; ++w)
					candidate_segments += __builtin_popcountll(cand_mask[w]);
			}
		}

		for (size_t ei = 0; ei < excluded_terms.size(); ++ei) {
			const int32_t exTermId = excluded_terms[ei];
			const auto& exChunksForTerm = (*matchTracker)[exTermId];
			excl_chunks_total += exChunksForTerm.size();

			for (size_t ci = 0; ci < exChunksForTerm.size(); ++ci) {
				const int32_t chunkID = exChunksForTerm[ci];

				if (segFilter && segDisjoint(chunkID)) {
					++seg_skips_not;
					continue;
				}

				const std::vector<int32_t>& files = get_files_for_chunk(chunkID);

				for (size_t fi = 0; fi < files.size(); ++fi) {
					const int32_t fid = files[fi];
					uint16_t* cntPtr = fileState.getIfExists(fid);
					if (!cntPtr) continue;

					// only survivors matter
					if (*cntPtr == (uint16_t)numIncluded) {
						*cntPtr = 0;     // killed by NOT
						if (--alive == 0) {
							break;
						}
					}
				}
				if (alive == 0) break;
			}
			if (alive == 0) break;
		}

		// --- Build final results from candidates (avoid re-scanning all segments) ---
		results.reserve(alive);
		for (size_t i = 0; i < candidates.size(); ++i) {
			int32_t fid = candidates[i];
			uint16_t* cntPtr = fileState.getIfExists(fid);
			if (cntPtr && *cntPtr == (uint16_t)numIncluded) {
				results.push_back(std::to_string(fid));
			}
		}

		TIMER_END(4, total_not_time);   // NOT filter + gather phase ends here
		majflt_not = majflt() - majflt_not;

		// --- Optional stats ---
		std::cout << "Unique chunks fetched: " << num_chunks_fetched
				<< ", Total files fetched: " << num_files_fetched
				<< ", matchTracker bytes: " << matchTracker_total_bytes
				<< ", fileState bytes: " << fileState.memoryUsageBytes()
				<< ", chunkToFiles buckets: " << chunkToFiles.bucket_count()
				<< std::endl;

		if (seg_bitmask != nullptr) {
			size_t seg_filter_skips = seg_skips_and + seg_skips_not;
			double skip_pct = excl_chunks_total ? 100.0 * seg_skips_not / excl_chunks_total : 0.0;
			double and_skip_pct = and_eligible_chunks ? 100.0 * seg_skips_and / and_eligible_chunks : 0.0;
			printf("Candidate segments: %d\n", candidate_segments);
			printf("Segment format: %s, words: %d, files/seg: %d\n",
			       segSparse ? "sparse" : "dense", segW, segFPS);
			printf("Included chunks total: %zu\n", included_chunks_total);
			printf("AND-eligible chunks: %zu\n", and_eligible_chunks);
			printf("Excluded chunks total: %zu\n", excl_chunks_total);
			printf("Seg filter skips: %zu\n", seg_filter_skips);
			printf("Seg filter skips AND: %zu\n", seg_skips_and);
			printf("Seg filter skips NOT: %zu\n", seg_skips_not);
			printf("Seg filter skip pct: %.2f\n", skip_pct);
			printf("Seg filter AND skip pct: %.2f\n", and_skip_pct);
		}
		printf("AND major faults: %ld\n", majflt_and);
		printf("NOT major faults: %ld\n", majflt_not);
		printf("Sorting included terms time: %.3f\n", total_sorting_keywords_time / 1000000.0);
		printf("AND evaluation time: %.3f\n", total_and_time / 1000000.0);
		printf("NOT evaluation time: %.3f\n", total_not_time / 1000000.0);
		printf("AND+NOT evaluation time: %.3f\n", (total_and_time + total_not_time) / 1000000.0);
		printf("Fetch files time: %.3f\n", (chunkFiles.doc_store_time + chunkFiles.reverse_mapping_time()) / 1000000.0);
		printf("Reverse mapping time: %.3f\n", chunkFiles.reverse_mapping_time() / 1000000.0);
		printf("Reverse mapping open time: %.3f\n", chunkFiles.open_time / 1000000.0);

		return results;
	}
	
	virtual std::map<std::string, double> getResultsFromSearcherWithScores(IndexSearcherPtr searcher) {
		std::map<std::string, double> results;
		
		for (size_t i = 0; i < m_docIds.size(); ++i) {
			auto doc = m_docIds[i];
			double score = m_scores[i];
			DocumentPtr document  = searcher->doc(doc);
			const auto& filename = document->get(toWstring("name"));
			results.emplace(StringUtils::toUTF8(filename), score);
		}

		return results;
	}
	
	virtual std::map<std::string, uint32_t> getResultsFromSearcherWithTermFreqs(IndexSearcherPtr searcher) {
		std::map<std::string, uint32_t> results;
		
		for (size_t i = 0; i < m_docIds.size(); ++i) {
			auto doc = m_docIds[i];
			uint32_t freq = m_termFreqs[i];
			DocumentPtr document  = searcher->doc(doc);
			const auto& filename = document->get(toWstring("name"));
			results.emplace(StringUtils::toUTF8(filename), freq);
		}

		return results;
	}

	virtual std::map<std::string, std::vector<uint64_t>> getResultsFromSearcherWithOffsets(IndexSearcherPtr searcher, std::vector<std::string> keywords) {
		std::map<std::string, std::vector<uint64_t>> results;
		
		for (auto doc : m_docIds) {
			DocumentPtr document  = searcher->doc(doc);
			for (const auto& filename : document->getValues(toWstring("name"))) {
				std::string name = StringUtils::toUTF8(filename);
				std::vector<uint64_t> accumulatedOffsets;
				TermPositionVectorPtr tv = 
					boost::dynamic_pointer_cast<TermPositionVector>(searcher->getIndexReader()->getTermFreqVector(doc, toWstring("data")));
				for (const auto& keyword : keywords) {
					uint32_t idx = tv->indexOf(toWstring(keyword));
					auto offsets = tv->getOffsets(idx);
					for (const auto& offsetObj : offsets) {
						accumulatedOffsets.push_back(offsetObj->getStartOffset());
					}
				}
				results.emplace(name, accumulatedOffsets);
			}
		}

		return results;
	}
};

using MyHitCollectorPtr = boost::shared_ptr<MyHitCollector>;

AbstractField::TermVector getTermVectorMode() {
	AbstractField::TermVector termVectorMode = AbstractField::TERM_VECTOR_NO;
	switch (destor.offsets_mode)
	{
	default:
	case NO_OFFSETS: termVectorMode = AbstractField::TERM_VECTOR_NO; break;
	case TERM_VECTOR_OFFSETS: termVectorMode = AbstractField::TERM_VECTOR_WITH_OFFSETS; break;
	}
	return termVectorMode;
}


DocumentPtr create_document(const std::wstring& name, const std::wstring& data) {


	FieldPtr nameField = newLucene<Field>(L"name", 
										  name, 
										  AbstractField::STORE_YES,
										  AbstractField::INDEX_NO, 
										  AbstractField::TERM_VECTOR_NO);
	FieldPtr dataField = newLucene<Field>(L"data", 
										  data, 
										  AbstractField::STORE_NO,
										  destor.tf_idf ? AbstractField::INDEX_ANALYZED : 
										  				AbstractField::INDEX_ANALYZED_NO_NORMS, 
										  getTermVectorMode());
	nameField->setOmitTermFreqAndPositions(true);
	if (destor.offsets_mode != GLOBAL_OFFSETS && !destor.tf_idf) {
		dataField->setOmitTermFreqAndPositions(true);
	}
	DocumentPtr document = newLucene<Document>();
	document->add(nameField);
	document->add(dataField);
	return document;
}

DocumentPtr create_fingerprint_document(std::wstring& fp, const std::wstring& data) {
	FieldPtr fpField = newLucene<Field>(L"fp", 
										  fp, 
										  AbstractField::STORE_NO,
										  AbstractField::INDEX_ANALYZED_NO_NORMS, 
										  AbstractField::TERM_VECTOR_NO);
	FieldPtr dataField = newLucene<Field>(L"data", 
										  data, 
										  AbstractField::STORE_NO,
										  AbstractField::INDEX_ANALYZED_NO_NORMS, 
										  getTermVectorMode());
	fpField->setOmitTermFreqAndPositions(true);
	dataField->setOmitTermFreqAndPositions(true);
	DocumentPtr document = newLucene<Document>();
	document->add(fpField);
	document->add(dataField);
	return document;
}

DocumentPtr create_multiple_names_document(const std::vector<std::wstring>& names, const std::wstring& data) {
	DocumentPtr document = newLucene<Document>();
	for (const auto& name : names) {
		FieldPtr nameField = newLucene<Field>(L"name", 
											name, 
											AbstractField::STORE_YES,
											AbstractField::INDEX_NO, 
											AbstractField::TERM_VECTOR_NO);
		nameField->setOmitTermFreqAndPositions(true);
		document->add(nameField);
	}
	FieldPtr dataField = newLucene<Field>(L"data", 
										  data, 
										  AbstractField::STORE_NO,
										  AbstractField::INDEX_ANALYZED_NO_NORMS, 
										  getTermVectorMode());
	dataField->setOmitTermFreqAndPositions(true);
	document->add(dataField);
	return document;
}

DocumentPtr create_names_only_document(const std::vector<std::wstring>& names) {
	DocumentPtr document = newLucene<Document>();
	for (const auto& name : names) {
		FieldPtr nameField = newLucene<Field>(L"name", 
											name, 
											AbstractField::STORE_YES,
											AbstractField::INDEX_NO, 
											AbstractField::TERM_VECTOR_NO);
		nameField->setOmitTermFreqAndPositions(true);
		document->add(nameField);
	}
	return document;
}

IndexWriterPtr get_index_writer(const std::string& index_path) {
	bool create = (access(index_path.c_str(), F_OK) != 0);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	auto writer =  newLucene<IndexWriter>(FSDirectory::open(toWstring(index_path)), 
										  get_analyzer(), 
										  create, 
										  std::numeric_limits<int>::max());
	writer->setSimilarity(getStaticMySimilarity());
	return writer;
}

// The dedup NOT pipeline evaluates included terms conjunctively -- it has no
// way to express "optional".  The parser keeps Lucene's standard OR default, so
// a conjunctive query must mark its included terms MUST explicitly:
//     +a +b +c -d        NOT        a b c -d
// An unmarked term parses as SHOULD and would be intersected anyway, silently
// answering a different query than the one asked; say so instead.  This is the
// check that would have caught the naive/dedup mismatch in B2.
static void warn_if_optional_clause(bool saw_optional) {
	if (saw_optional) {
		std::wcout << L"WARNING: query contains optional (SHOULD) clauses; the dedup "
		           << L"NOT pipeline intersects all included terms, so the result "
		           << L"corresponds to an AND query." << std::endl;
	}
}

std::vector<std::string> search_index(const std::string& query, const std::string& index, SyncQueue* queue) {
	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	QueryParserPtr parser = newLucene<QueryParser>(LuceneVersion::LUCENE_30,
												   toWstring("data"),
												   get_analyzer());

	return search_query_in_index(parser->parse(toWstring(query)), index, queue);
}

std::vector<std::string> search_index_complex_query(const std::string& query, const std::string& index, SyncQueue* queue) {	
	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	QueryParserPtr parser = newLucene<QueryParser>(LuceneVersion::LUCENE_30,
												   toWstring("data"),
												   get_analyzer());
	QueryPtr q = parser->parse(toWstring(query));
	std::wcout << "query is: " << q->toString() << std::endl;
	Collection<BooleanClausePtr> parsedClauses = boost::static_pointer_cast<BooleanQuery>(q)->getClauses();
	BooleanQueryPtr new_query = newLucene<BooleanQuery>();
	std::vector<std::vector<int32_t>> docBitmaps(parsedClauses.size());
	std::vector<int32_t> included_terms;
	std::vector<int32_t> excluded_terms;
	bool saw_optional = false;
	for (size_t i = 0; i < parsedClauses.size(); i++) {
		String termText = boost::static_pointer_cast<TermQuery>(parsedClauses[i]->getQuery())->getTerm()->text();
		uint16_t occur = parsedClauses[i]->getOccur();
		new_query->add(newLucene<TrackingTermQuery2>(newLucene<Term>(toWstring("data"), termText), i, &docBitmaps),
					   BooleanClause::SHOULD);
		if (occur != BooleanClause::MUST_NOT) {
			included_terms.push_back(i);
			saw_optional |= (occur == BooleanClause::SHOULD);
		} else {
			excluded_terms.push_back(i);
		}
		std::wcout << "Term: " << termText << " Occur: " << occur << std::endl;
	}
	warn_if_optional_clause(saw_optional);
	return search_query_in_index_not(new_query, included_terms, excluded_terms, index, &docBitmaps, queue);
}

// Same as search_index_complex_query but loads the pre-built segment bitmask
// and passes it to the NOT filter to skip excluded chunks with no files in
// any candidate segment (avoiding reverse-mapping lookups for those chunks).
std::vector<std::string> search_index_complex_query_seg_filter(const std::string& query, const std::string& index, int files_per_seg, bool sparse, SyncQueue* queue) {
	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());
	QueryParserPtr parser = newLucene<QueryParser>(LuceneVersion::LUCENE_30,
	                                               toWstring("data"),
	                                               get_analyzer());
	QueryPtr q = parser->parse(toWstring(query));
	std::wcout << "query is: " << q->toString() << std::endl;
	Collection<BooleanClausePtr> parsedClauses = boost::static_pointer_cast<BooleanQuery>(q)->getClauses();
	BooleanQueryPtr new_query = newLucene<BooleanQuery>();
	std::vector<std::vector<int32_t>> docBitmaps(parsedClauses.size());
	std::vector<int32_t> included_terms, excluded_terms;
	bool saw_optional = false;
	for (size_t i = 0; i < parsedClauses.size(); i++) {
		String termText = boost::static_pointer_cast<TermQuery>(parsedClauses[i]->getQuery())->getTerm()->text();
		uint16_t occur = parsedClauses[i]->getOccur();
		new_query->add(newLucene<TrackingTermQuery2>(newLucene<Term>(toWstring("data"), termText), i, &docBitmaps),
		               BooleanClause::SHOULD);
		if (occur != BooleanClause::MUST_NOT) {
			included_terms.push_back(i);
			saw_optional |= (occur == BooleanClause::SHOULD);
		} else {
			excluded_terms.push_back(i);
		}
	}
	warn_if_optional_clause(saw_optional);

	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0, seg_build_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1); TIMER_DECLARE(2); TIMER_DECLARE(3); TIMER_DECLARE(4); TIMER_DECLARE(5);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(5);
		const SegBitmask& seg_bitmask = loadSegmentBitmask(index, files_per_seg, sparse);
		TIMER_END(5, seg_build_time);

		// This query is the filtered one, so running it without its segment
		// index would only look like a result; and one built for another state
		// of the index would skip chunks on behalf of files it does not know.
		if (seg_bitmask.empty() && searcher->maxDoc() > 0) {
			fprintf(stderr, "no usable segment index; build it with 'destor -o -P %d%s'\n",
			        files_per_seg, sparse ? " -S" : "");
			exit(1);
		}
		if (!seg_bitmask.empty() && seg_bitmask.size() != (size_t)searcher->maxDoc()) {
			fprintf(stderr, "%s covers %zu chunks but the index has %d; rebuild it with 'destor -o -P %d%s'\n",
			        segBitmaskPath(index, seg_bitmask.files_per_seg, sparse).c_str(),
			        seg_bitmask.size(), searcher->maxDoc(), seg_bitmask.files_per_seg, sparse ? " -S" : "");
			exit(1);
		}

		TIMER_BEGIN(1);
		searcher->search(new_query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		TIMER_BEGIN(2);
		results = collector->getResultsFromSearcherInclExcl(
		    searcher, &docBitmaps, included_terms, excluded_terms,
		    seg_bitmask.empty() ? nullptr : &seg_bitmask);
		TIMER_END(2, doc_fetch_lookup_time);
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("seg filter load time: %.3f\n", seg_build_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time / 1000000);
	return results;
}

std::vector<std::string> search_keywords_in_index(const std::vector<std::string>& keywords, 
												  const std::string& index, 
												  SyncQueue* queue) {

	double query_creation_time = 0;
	TIMER_DECLARE(1);
	TIMER_BEGIN(1);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>();	

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))),
				   BooleanClause::SHOULD);
	}

	TIMER_END(1, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	return search_query_in_index(query, index, queue);
}

std::vector<std::string> search_keywords_in_index_and(const std::vector<std::string>& keywords, 
												  const std::string& index, 
												  SyncQueue* queue) {

	double query_creation_time = 0;
	TIMER_DECLARE(1);
	TIMER_BEGIN(1);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>();	

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))),
				   BooleanClause::MUST);
	}

	TIMER_END(1, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	return search_query_in_index(query, index, queue);
}

std::vector<std::string> search_must_keywords_in_index_bitmap(const std::vector<std::string>& keywords, 
												  const std::string& index, 
												  SyncQueue* queue) {

	double query_creation_time = 0;
	TIMER_DECLARE(1);
	TIMER_BEGIN(1);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>();
	std::unordered_map<int32_t, boost::dynamic_bitset<>> docBitmaps;

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TrackingTermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword)), i, keywords.size(), &docBitmaps),
				   BooleanClause::SHOULD);
	}


	TIMER_END(1, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	return search_query_in_index_tracking_bitmap(query, index, &docBitmaps, keywords.size(), queue);
}

std::vector<std::string> search_must_keywords_in_index_incremental(const std::vector<std::string>& keywords, 
												   const std::string& index, 
												   SyncQueue* queue) {

	double query_creation_time = 0;
	TIMER_DECLARE(1);
	TIMER_BEGIN(1);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>();
	std::vector<std::vector<int32_t>> docBitmaps(keywords.size());

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TrackingTermQuery2>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword)), i, &docBitmaps),
				   BooleanClause::SHOULD);
	}


	TIMER_END(1, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	return search_query_in_index_tracking_vector(query, index, &docBitmaps, queue);
}

std::vector<std::string> search_query_in_index(const QueryPtr& query, const std::string& index, SyncQueue* queue) {
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		if (destor.is_threaded_lookup) { 
			results = collector->m_results;
		} else {
			TIMER_BEGIN(2);
			results = collector->getResultsFromSearcher(searcher);
			TIMER_END(2, doc_fetch_lookup_time);
		}
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	return results;
}

std::vector<std::string> search_query_in_index_tracking_bitmap(const QueryPtr& query, const std::string& index, std::unordered_map<int32_t, boost::dynamic_bitset<>>* docBitmaps, size_t num_keywords, SyncQueue* queue) {
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		TIMER_BEGIN(2);
		if (destor.is_threaded_lookup) { 
			results = collector->m_results;
		} else {
			results = collector->getResultsFromSearcherWithTrackingBitmap(searcher, docBitmaps, num_keywords);
		}
		TIMER_END(2, doc_fetch_lookup_time);
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	return results;
}

struct IoStats {
    long long read_bytes  = 0;
    long long write_bytes = 0;
};

IoStats getIoStats() {
    IoStats s;
    std::ifstream f("/proc/self/io");
    std::string key;
    long long value;
    while (f >> key >> value) {
        if (key == "read_bytes:")  s.read_bytes = value;
        if (key == "write_bytes:") s.write_bytes = value;
    }
    return s;
}

std::vector<std::string> search_query_in_index_tracking_vector(const QueryPtr& query, const std::string& index, std::vector<std::vector<int32_t>>* docBitmaps, SyncQueue* queue) {
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);
		IoStats before = getIoStats();
		TIMER_BEGIN(2);
		if (destor.is_threaded_lookup) {
			results = collector->m_results;
		} else {
			switch (destor.and_impl) {
			case AND_IMPL_INCREMENTAL:
			default:
				results = collector->getResultsFromSearcherWithTrackingVector(searcher, docBitmaps);
				break;
			}
		}
		TIMER_END(2, doc_fetch_lookup_time);
		IoStats after = getIoStats();
		std::cout << "Query I/O: read_bytes = " << (after.read_bytes - before.read_bytes)
          << ", write_bytes = " << (after.write_bytes - before.write_bytes)
          << std::endl;
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	return results;
}

std::vector<std::string> search_query_in_index_not(const QueryPtr& query, std::vector<int32_t> included_terms, std::vector<int32_t> excluded_terms, const std::string& index, std::vector<std::vector<int32_t>>* docBitmaps, SyncQueue* queue) {
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);
		IoStats before = getIoStats();
		TIMER_BEGIN(2);
		if (destor.is_threaded_lookup) { 
			results = collector->m_results;
		} else {
			results = collector->getResultsFromSearcherInclExcl(searcher, docBitmaps, included_terms, excluded_terms);
		}
		TIMER_END(2, doc_fetch_lookup_time);
		IoStats after = getIoStats();
		std::cout << "Query I/O: read_bytes = " << (after.read_bytes - before.read_bytes)
          << ", write_bytes = " << (after.write_bytes - before.write_bytes)
          << std::endl;
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	return results;
}

std::map<std::string, std::vector<uint64_t>> search_keywords_in_index_with_tv_offsets(const std::vector<std::string>& keywords, const std::string& index, SyncQueue* queue = nullptr) {
	
	double query_creation_time = 0;
	TIMER_DECLARE(7);
	TIMER_BEGIN(7);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>();	

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))),
				   BooleanClause::SHOULD);
	}

	TIMER_END(7, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::map<std::string, std::vector<uint64_t>> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		TIMER_BEGIN(2);
		results = collector->getResultsFromSearcherWithOffsets(searcher, keywords);
		TIMER_END(2, doc_fetch_lookup_time);
		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	
	return results;
}

std::map<std::string, double> search_keywords_in_index_with_scores(const std::vector<std::string>& keywords, const std::string& index, SyncQueue* queue = nullptr) {
	
	double query_creation_time = 0;
	TIMER_DECLARE(7);
	TIMER_BEGIN(7);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>(false);	

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))),
				   BooleanClause::SHOULD);
	}

	TIMER_END(7, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::map<std::string, double> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		TIMER_BEGIN(2);
		results = collector->getResultsFromSearcherWithScores(searcher);
		TIMER_END(2, doc_fetch_lookup_time);

		// std::wcout << searcher->explain(query, collector->m_docIds[0])->toString() << std::endl;

		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	
	return results;
}


std::map<std::string, uint32_t> search_keywords_in_index_with_termfreqs(const std::vector<std::string>& keywords, const std::string& index, SyncQueue* queue = nullptr) {
	
	double query_creation_time = 0;
	TIMER_DECLARE(7);
	TIMER_BEGIN(7);

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());

	BooleanQueryPtr query = newLucene<BooleanQuery>(false);	

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		query->add(newLucene<TermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))),
				   BooleanClause::SHOULD);
	}

	TIMER_END(7, query_creation_time);
	printf("query creation time: %.3f\n", query_creation_time / 1000000);
	
	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0;
	std::map<std::string, uint32_t> results;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	TIMER_DECLARE(3);
	TIMER_DECLARE(4);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(1);
		searcher->search(query, nullptr, collector);
		TIMER_END(1, doc_id_lookup_time);

		TIMER_BEGIN(2);
		results = collector->getResultsFromSearcherWithTermFreqs(searcher);
		TIMER_END(2, doc_fetch_lookup_time);

		// std::wcout << searcher->explain(query, collector->m_docIds[0])->toString() << std::endl;

		TIMER_BEGIN(4);
	}
	TIMER_END(4, dtor_time);
	dtor_time /= 1000000;
	printf("searcher load time: %.3f\n", searcher_load_time / 1000000);
	printf("doc lookup time: %.3f\n", doc_id_lookup_time / 1000000);
	printf("doc fetch lookup time: %.3f\n", doc_fetch_lookup_time / 1000000);
	printf("destructors' time: %.3f\n", dtor_time);
	
	return results;
}


std::map<std::string, std::vector<uint64_t>> search_keywords_in_index_with_global_offsets(const std::vector<std::string>& keywords, const std::string& index, SyncQueue* queue = nullptr) {
	std::map<uint32_t, std::vector<uint64_t>> results_docid;
	std::map<std::string, std::vector<uint64_t>> results;

	BooleanQuery::setMaxClauseCount(std::numeric_limits<int32_t>::max());
	Collection<SpanQueryPtr> clauses = Collection<SpanQueryPtr>::newInstance();

	for (size_t i = 0; i < keywords.size(); i++) {
		const String keyword = toWstring(keywords[i]);
		clauses.add(newLucene<SpanTermQuery>(newLucene<Term>(toWstring("data"), 
														StringUtils::toLower(keyword))));
	}

	SpanOrQueryPtr query = newLucene<SpanOrQuery>(std::move(clauses));

	auto indexReader = IndexReader::open(FSDirectory::open(toWstring(index)));

	SpansPtr spans = query->getSpans(indexReader);

	int64_t current_doc_id = -1;
	std::vector<uint64_t> doc_offsets;

	while (spans->next()) {
		if (spans->doc() != current_doc_id) {
			if (current_doc_id != -1) {
				results_docid.emplace(current_doc_id, std::move(doc_offsets));
				doc_offsets = std::vector<uint64_t>();
			}
			current_doc_id = spans->doc();
		}

		doc_offsets.push_back(spans->start());
	}

	if (current_doc_id != -1) {
		results_docid.emplace(current_doc_id, std::move(doc_offsets));
	}

	for (auto& pair : results_docid) {
		auto document = indexReader->document(pair.first);
		std::string docName = StringUtils::toUTF8(document->get(toWstring("name")));
		results.emplace(docName, std::move(pair.second));
	}

	return results;
}

void create_directories(const std::string& path) {
    std::string partial;
    for (char ch : path) {
        partial += ch;
        if (ch == '/' && access(partial.c_str(), F_OK) != 0) {
            mkdir(partial.c_str(), 0755);
        }
    }
    if (access(path.c_str(), F_OK) != 0) {
        mkdir(path.c_str(), 0755);
    }
}

void print_terms(const std::string& index) {
	IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
    auto indexReader = searcher->getIndexReader();
    TermEnumPtr terms = indexReader->terms();

    std::vector<TermPtr> candidates;

    while (terms->next()) {
        int freq = terms->docFreq();
		// Low - 1000 - 10,000
		// Med - 20,000 - 50,000
		// High - 100,000 - 500,000
        if (freq >= 20000 && freq <= 50000) {
            candidates.push_back(terms->term());
        }
    }
    terms->close();

    // Shuffle the valid terms
    auto seed = std::chrono::high_resolution_clock::now().time_since_epoch().count();
    std::shuffle(candidates.begin(), candidates.end(), std::default_random_engine(seed));

    std::vector<int> sampleSizes = {4, 8, 16, 32, 64, 128};
    std::string basePath = "keywords/linux/test/file_med";

    // Ensure directory exists
    create_directories(basePath);

    for (int size : sampleSizes) {
        std::string filePath = basePath + "/keywords_" + std::to_string(size) + ".txt";
        std::wofstream out(filePath);
        out.imbue(std::locale("en_US.UTF-8"));  // Ensure UTF-8 writing for wide characters

        if (!out) {
            std::cerr << "Failed to open file: " << filePath << std::endl;
            continue;
        }

        // out << L"# Random " << size << L" keywords with docFreq in [100,1000]\n";

        for (int i = 0; i < std::min(size, (int)candidates.size()); ++i) {
            auto term = candidates[i];
            int freq = indexReader->docFreq(term);
            // out << L"term=" << term->text() << L" freq=" << freq << L"\n";
			out << term->text() << L"\n";
        }

        out.close();
    }
}

AnalyzerPtr get_analyzer() {
	static bool is_init = false;
	if (!is_init) {
		is_init = true;
		STOP_LIST = HashSet<String>::newInstance(_STOP_LIST, _STOP_LIST + SIZEOF_ARRAY(_STOP_LIST));
	}

	return newLucene<MyAnalyzer>(LuceneVersion::LUCENE_30, STOP_LIST);
}

IndexWriterPtr protectedAddDocument(IndexWriterPtr writer, DocumentPtr document, const std::string& index_path) {
	try {
		writer->addDocument(document, get_analyzer());
		return writer;
	} catch (std::bad_alloc&) {
		printf("Encountered bad alloc during document addition!\n");
		writer->close();
		auto newWriter = get_index_writer(index_path); 
		newWriter->addDocument(document, get_analyzer());
		return newWriter;
	}
}

void load_only(std::string path) {
	double total_time = 0;
	double cleanup_time = 0;
	TIMER_DECLARE(1);
	TIMER_DECLARE(2);
	{
		TIMER_BEGIN(1);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(path)));
		
		TIMER_END(1, total_time);
		printf("load time: %.3f\n", total_time / 1000000);
		fflush(stdout);
		
		TIMER_BEGIN(2);
	}
	TIMER_END(2, cleanup_time);
	printf("cleanup time: %.3f\n", cleanup_time / 1000000);
}

uint32_t getLastNumTokens() {
	return getStaticMySimilarity()->lastNumTokens;
}