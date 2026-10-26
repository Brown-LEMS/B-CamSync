#pragma once
#include <cstdint>
extern "C" {
char* bcamsync_rust_config(const char* json, int operation);
int bcamsync_rust_clock_ns(int kind, std::int64_t* output);
char* bcamsync_rust_map_time(int valid, std::int64_t offset, std::int64_t local);
void bcamsync_rust_string_free(char* value);
}
