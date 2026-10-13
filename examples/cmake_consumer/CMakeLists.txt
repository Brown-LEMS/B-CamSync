cmake_minimum_required(VERSION 3.18)
project(BCamSyncInstalledConsumer LANGUAGES CXX)

find_package(BCamSync CONFIG REQUIRED)
add_executable(consumer main.cpp)
target_compile_features(consumer PRIVATE cxx_std_17)
target_compile_options(consumer PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(consumer PRIVATE BCamSync::bcamsync)
