#include "libclucene-wrapper.h"
#include "destor.h"
#include "pathstore.h"
#include "TrackingTermScorer.h"
#include "TrackingTermScorer2.h"
#include <boost/dynamic_bitset.hpp>

#include <sys/stat.h>
#include <sys/types.h>
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

static const uint32_t FILES_PER_SEG = 1024;

// Build a per-chunk segment bitmask from the Lucene index.
// bitmask[docId] has bit s set iff segment s (files [s*FILES_PER_SEG, (s+1)*FILES_PER_SEG))
// contains at least one file that includes that chunk.
static std::vector<uint64_t> buildSegmentBitmask(IndexReaderPtr reader) {
    int32_t maxDoc = reader->maxDoc();
    std::vector<uint64_t> bitmask(maxDoc, 0ULL);
    static const std::wstring nameField = toWstring("name");
    for (int32_t docId = 0; docId < maxDoc; ++docId) {
        if (reader->isDeleted(docId)) continue;
        DocumentPtr doc = reader->document(docId);
        const auto& fileIDs = doc->getValues(nameField);
        uint64_t mask = 0ULL;
        for (size_t i = 0; i < fileIDs.size(); ++i) {
            uint32_t seg = (uint32_t)std::stoi(fileIDs[i]) / FILES_PER_SEG;
            if (seg < 64) mask |= (1ULL << seg);
        }
        bitmask[docId] = mask;
    }
    return bitmask;
}

// Build and persist the segment bitmask alongside the index.
// Called once at indexing time so queries don't need to touch the reverse mapping.
void saveSegmentBitmask(const std::string& index_path) {
    auto reader = IndexReader::open(FSDirectory::open(toWstring(index_path)));
    std::vector<uint64_t> bitmask = buildSegmentBitmask(reader);
    reader->close();
    std::string path = index_path + "/seg_bitmask.bin";
    FILE* f = fopen(path.c_str(), "wb");
    if (!f) {
        fprintf(stderr, "saveSegmentBitmask: cannot write %s\n", path.c_str());
        return;
    }
    fwrite(bitmask.data(), sizeof(uint64_t), bitmask.size(), f);
    fclose(f);
    printf("Segment bitmask saved: %zu entries, %.1f MB → %s\n",
           bitmask.size(), bitmask.size() * 8.0 / (1 << 20), path.c_str());
}

// Load the pre-built segment bitmask from disk (flat sequential read).
static std::vector<uint64_t> loadSegmentBitmask(const std::string& index_path) {
    std::string path = index_path + "/seg_bitmask.bin";
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) {
        fprintf(stderr, "loadSegmentBitmask: %s not found — run destor -o first\n", path.c_str());
        return {};
    }
    fseek(f, 0, SEEK_END);
    long bytes = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::vector<uint64_t> bitmask((size_t)bytes / sizeof(uint64_t));
    fread(bitmask.data(), sizeof(uint64_t), bitmask.size(), f);
    fclose(f);
    return bitmask;
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
		TIMER_DECLARE(5);
		TIMER_DECLARE(6);
		TIMER_DECLARE(7);
		double total_accessing_docs_in_bitmap_time = 0, total_docs_file_marking_time = 0, total_docs_fetch_files_time = 0, file_coverage_check_time = 0;
		std::vector<std::string> results;
		std::unordered_map<int32_t, boost::dynamic_bitset<>> fileBitmaps;

		std::cout << "Number of chunks: " << m_docIds.size() << ", " << docBitmaps->size() << std::endl;

		static const std::wstring nameField = toWstring("name");

		for (auto doc : m_docIds) {
			TIMER_BEGIN(7);
			auto it = docBitmaps->find(doc);
			if (it == docBitmaps->end()) {
				std::cerr << "Warning: docBitmaps missing entry for doc " << doc << std::endl;		
				continue;
			}
			const auto& chunkBitmap = it->second;
			TIMER_END(7, total_accessing_docs_in_bitmap_time);

			TIMER_BEGIN(5);
			DocumentPtr document = searcher->doc(doc);
			const auto& filenames = document->getValues(nameField);
			TIMER_END(5, total_docs_fetch_files_time);
			std::cout << "doc: " << doc << std::endl;
			TIMER_BEGIN(2);
			for (const auto& filename : filenames) {
				std::cout << "file: " << StringUtils::toUTF8(filename) << std::endl;
				std::string utf8Filename = StringUtils::toUTF8(filename);
				int file_id = std::stoi(utf8Filename);

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
		printf("fetch files time: %.3f\n", total_docs_fetch_files_time / 1000000);
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
    const std::vector<uint64_t>* seg_bitmask = nullptr)
	{
		TIMER_DECLARE(1);
		TIMER_DECLARE(2);
		TIMER_DECLARE(3);

		double total_sorting_keywords_time = 0;
		double total_intersection_time     = 0;
		double total_fetch_files_time      = 0;

		std::vector<std::string> results;
		bool no_common_files = false;

		uint32_t num_files_fetched  = 0;
		uint32_t num_chunks_fetched = 0;
		size_t   seg_filter_skips   = 0;

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

		static const std::wstring nameField = toWstring("name");

		// --- Caches / state ---
		ChunkToFilesMap chunkToFiles;   // chunkID -> vector<int32_t> fileIds
		SegmentedFileCounts fileState;  // fileId  -> seenCount

		// Helper: fetch and cache file IDs for a chunk
		auto get_files_for_chunk = [&](int32_t chunkID) -> const std::vector<int32_t>& {
			auto it = chunkToFiles.find(chunkID);
			if (it != chunkToFiles.end()) {
				return it->second; // cache hit
			}

			TIMER_BEGIN(3);
			DocumentPtr document = searcher->doc(chunkID);
			const auto& fileIDs  = document->getValues(nameField);
			TIMER_END(3, total_fetch_files_time);

			++num_chunks_fetched;

			std::vector<int32_t> files;
			files.reserve(fileIDs.size());
			for (size_t i = 0; i < fileIDs.size(); ++i) {
				const std::wstring& w = fileIDs[i];
				int32_t fileId = std::stoi(w);
				files.push_back(fileId);
			}
			num_files_fetched += (uint32_t)files.size();

			auto inserted = chunkToFiles.insert(std::make_pair(chunkID, std::move(files)));
			return inserted.first->second;
		};

		// --- AND (Count-Advancing) over INCLUDED terms ---
		TIMER_BEGIN(2);

		const int32_t numIncluded = (int32_t)sorted_termIDs_by_matches.size();
		if (numIncluded == 0) {
			// Pure NOT is undefined without a universe of all files.
			TIMER_END(2, total_intersection_time);
			return results;
		}

		for (int32_t termIndex = 0; termIndex < numIncluded; ++termIndex) {
			int32_t termId = sorted_termIDs_by_matches[termIndex];
			size_t advancedThisTerm = 0;

			const auto& chunksForTerm = (*matchTracker)[termId];
			for (size_t ci = 0; ci < chunksForTerm.size(); ++ci) {
				int32_t chunkID = chunksForTerm[ci];
				const std::vector<int32_t>& files = get_files_for_chunk(chunkID);

				if (termIndex == 0) {
					for (size_t fi = 0; fi < files.size(); ++fi) {
						int32_t fid = files[fi];
						uint16_t& cnt = fileState.getOrCreate(fid);
						if (cnt == 0) cnt = 1;
					}
				} else {
					for (size_t fi = 0; fi < files.size(); ++fi) {
						int32_t fid = files[fi];
						uint16_t* cntPtr = fileState.getIfExists(fid);
						if (!cntPtr) continue;
						if (*cntPtr == (uint16_t)termIndex) {
							*cntPtr = (uint16_t)(termIndex + 1);
							++advancedThisTerm;
						}
					}
				}
			}

			if (termIndex > 0 && advancedThisTerm == 0) {
				no_common_files = true;
				break;
			}
		}

		// If AND already empty -> done
		if (no_common_files) {
			TIMER_END(2, total_intersection_time);
			return results;
		}

		// --- Collect AND survivors as int32 candidates ---
		std::vector<int32_t> candidates;
		candidates.reserve(1024); // optional
		fileState.collectResultsInt32((uint16_t)numIncluded, candidates);

		if (candidates.empty()) {
			TIMER_END(2, total_intersection_time);
			return results;
		}

		// --- NOT filter: kill any AND-survivor that appears in excluded terms ---
		size_t alive = candidates.size();

		// Segment filter: candidate_mask has bit s set iff any candidate file is in segment s.
		// An excluded chunk can be skipped entirely if none of its segments overlap with candidates.
		uint64_t candidate_mask = 0;
		if (seg_bitmask != nullptr) {
			for (int32_t fid : candidates) {
				uint32_t seg = (uint32_t)fid / FILES_PER_SEG;
				if (seg < 64) candidate_mask |= (1ULL << seg);
			}
		}

		for (size_t ei = 0; ei < excluded_terms.size(); ++ei) {
			const int32_t exTermId = excluded_terms[ei];
			const auto& exChunksForTerm = (*matchTracker)[exTermId];

			for (size_t ci = 0; ci < exChunksForTerm.size(); ++ci) {
				const int32_t chunkID = exChunksForTerm[ci];

				if (seg_bitmask != nullptr && chunkID < (int32_t)seg_bitmask->size() &&
				    ((*seg_bitmask)[chunkID] & candidate_mask) == 0) {
					++seg_filter_skips;
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

		TIMER_END(2, total_intersection_time);

		// --- Optional stats ---
		std::cout << "Unique chunks fetched: " << num_chunks_fetched
				<< ", Total files fetched: " << num_files_fetched
				<< ", matchTracker bytes: " << matchTracker_total_bytes
				<< ", fileState bytes: " << fileState.memoryUsageBytes()
				<< ", chunkToFiles buckets: " << chunkToFiles.bucket_count()
				<< std::endl;

		if (seg_filter_skips > 0)
			printf("Seg filter skips: %zu\n", seg_filter_skips);
		printf("Sorting included terms time: %.3f\n", total_sorting_keywords_time / 1000000.0);
		printf("AND+NOT evaluation time: %.3f\n", total_intersection_time / 1000000.0);
		printf("Fetch files time: %.3f\n", total_fetch_files_time / 1000000.0);

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
	for (size_t i = 0; i < parsedClauses.size(); i++) {
		String termText = boost::static_pointer_cast<TermQuery>(parsedClauses[i]->getQuery())->getTerm()->text();
		uint16_t occur = parsedClauses[i]->getOccur();
		new_query->add(newLucene<TrackingTermQuery2>(newLucene<Term>(toWstring("data"), termText), i, &docBitmaps),
					   BooleanClause::SHOULD);
		if (occur != BooleanClause::MUST_NOT) {
			included_terms.push_back(i);
		} else {
			excluded_terms.push_back(i);
		}
		std::wcout << "Term: " << termText << " Occur: " << occur << std::endl;
	}
	return search_query_in_index_not(new_query, included_terms, excluded_terms, index, &docBitmaps, queue);
}

// Same as search_index_complex_query but loads the pre-built segment bitmask
// and passes it to the NOT filter to skip excluded chunks with no files in
// any candidate segment (avoiding reverse-mapping lookups for those chunks).
std::vector<std::string> search_index_complex_query_seg_filter(const std::string& query, const std::string& index, SyncQueue* queue) {
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
	for (size_t i = 0; i < parsedClauses.size(); i++) {
		String termText = boost::static_pointer_cast<TermQuery>(parsedClauses[i]->getQuery())->getTerm()->text();
		uint16_t occur = parsedClauses[i]->getOccur();
		new_query->add(newLucene<TrackingTermQuery2>(newLucene<Term>(toWstring("data"), termText), i, &docBitmaps),
		               BooleanClause::SHOULD);
		if (occur != BooleanClause::MUST_NOT) included_terms.push_back(i);
		else                                   excluded_terms.push_back(i);
	}

	MyHitCollectorPtr collector = newLucene<MyHitCollector>(queue);
	double doc_id_lookup_time = 0, doc_fetch_lookup_time = 0, seg_build_time = 0;
	std::vector<std::string> results;
	TIMER_DECLARE(1); TIMER_DECLARE(2); TIMER_DECLARE(3); TIMER_DECLARE(4); TIMER_DECLARE(5);
	{
		TIMER_BEGIN(3);
		IndexSearcherPtr searcher = newLucene<IndexSearcher>(FSDirectory::open(toWstring(index)));
		TIMER_END(3, searcher_load_time);

		TIMER_BEGIN(5);
		std::vector<uint64_t> seg_bitmask = loadSegmentBitmask(index);
		TIMER_END(5, seg_build_time);

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