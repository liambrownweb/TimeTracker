cmake_minimum_required(VERSION 3.16)
project(TimeTrackerPlasma LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
find_package(Qt5 5.15 REQUIRED COMPONENTS Core Network Qml Test)

add_library(capture STATIC src/capture.cpp src/capture.h)
set_target_properties(capture PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_link_libraries(capture PUBLIC Qt5::Core Qt5::Network)
target_include_directories(capture PUBLIC src)
target_compile_options(capture PRIVATE -Wall -Wextra -Wpedantic)

add_library(timetrackerplugin MODULE src/plugin.cpp)
target_link_libraries(timetrackerplugin PRIVATE capture Qt5::Qml)
set_target_properties(timetrackerplugin PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/package/contents/ui/backend")
foreach(package_file metadata.json contents/ui/main.qml contents/ui/backend/qmldir)
    configure_file("package/${package_file}" "package/${package_file}" COPYONLY)
endforeach()

enable_testing()
add_executable(capture_tests tests/capture_tests.cpp)
target_link_libraries(capture_tests PRIVATE capture Qt5::Test Qt5::Qml)
add_dependencies(capture_tests timetrackerplugin)
add_test(NAME capture_tests COMMAND capture_tests)
