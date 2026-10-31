#ifndef _BERKELEY_DB_REVERSE_MAPPING_H_
#define _BERKELEY_DB_REVERSE_MAPPING_H_

#include "../destor.h"
#include <unordered_map>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <db.h>

#include "sync_queue.h"


void init_db_reverse_mapping();
void close_db_reverse_mapping();

void insert_db_reverse_mapping(const std::string& fp, const std::string& file_path);
void insert_db_reverse_mapping_offsets(const std::string& fp, const std::string& file_path, const std::vector<uint64_t>& offsets);
void insert_db_reverse_mapping_counter(const std::string& fp, const std::string& file_path, uint32_t count);
std::set<std::string> get_db_reverse_mapping_multiple(const std::vector<std::string>& fps, SyncQueue* queue = nullptr);
std::map<std::string, std::vector<uint64_t>> get_db_reverse_mapping_multiple_with_offsets(const std::map<std::string, std::vector<uint64_t>>& fps);
std::map<std::string, uint32_t> get_db_reverse_mapping_multiple_counter(const std::map<std::string, uint32_t>& fps);
void get_db_reverse_mapping_fetch(const std::string& fp, SyncQueue* queue);
void get_db_reverse_mapping_iterate(DBT* data, SyncQueue* queue);

// Read-only access to the reverse mapping, one fingerprint at a time, for
// evaluating a query chunk by chunk.  Each reader owns its handle, cursor and
// bulk buffer, independent of init_db_reverse_mapping().  The mapping must
// hold plain records (offsets-mode none, tf-idf no) of numeric file IDs
// (indirect-path-strings yes).
class DbReverseMappingReader {
public:
    DbReverseMappingReader();
    ~DbReverseMappingReader();
    DbReverseMappingReader(const DbReverseMappingReader&) = delete;
    DbReverseMappingReader& operator=(const DbReverseMappingReader&) = delete;

    // Appends the ID of each file stored under fp, once per occurrence of the
    // chunk in that file, as the in-doc chunk documents list them.  Returns
    // how many were appended; a fingerprint with no records has none.
    size_t get_file_ids(const std::string& fp, std::vector<int32_t>& out);

    // Microseconds spent reading from Berkeley DB.  Parsing the records is not
    // included, just as in-doc does not time converting its stored IDs.
    double read_time = 0;

private:
    DB* db;
    DBC* cursor;
    std::vector<char> buffer;
    std::vector<int32_t> ids;   // one lookup's IDs; keeps its capacity
};

#endif //_BERKELEY_DB_REVERSE_MAPPING_H_