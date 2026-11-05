# gcrt - GameCube/Wii static recompilation runtime.
#
# Used by generated projects via add_subdirectory(runtime) followed by
#   gcrt_add_game(<target> <generated sources...>)
cmake_minimum_required(VERSION 3.16)
project(gcrt C)

set(GCRT_PLATFORM "auto" CACHE STRING "Platform layer: auto, sdl, headless, android")
option(GCRT_FETCH_SDL "Download and build SDL2 if it is not installed" ON)

if(GCRT_PLATFORM STREQUAL "auto")
  if(ANDROID)
    set(GCRT_PLATFORM "android")
  else()
    find_package(SDL2 CONFIG QUIET)
    if(NOT SDL2_FOUND AND NOT CMAKE_CROSSCOMPILING)
      find_package(PkgConfig QUIET)
      if(PKG_CONFIG_FOUND)
        pkg_check_modules(SDL2PC QUIET IMPORTED_TARGET sdl2)
      endif()
    endif()
    if(SDL2_FOUND OR SDL2PC_FOUND OR GCRT_FETCH_SDL)
      set(GCRT_PLATFORM "sdl")
    else()
      set(GCRT_PLATFORM "headless")
    endif()
  endif()
endif()
message(STATUS "gcrt platform layer: ${GCRT_PLATFORM}")

set(GCRT_SOURCES
  ${CMAKE_CURRENT_LIST_DIR}/src/cpu.c
  ${CMAKE_CURRENT_LIST_DIR}/src/memory.c
  ${CMAKE_CURRENT_LIST_DIR}/src/exceptions.c
  ${CMAKE_CURRENT_LIST_DIR}/src/threads.c
  ${CMAKE_CURRENT_LIST_DIR}/src/hw.c
  ${CMAKE_CURRENT_LIST_DIR}/src/gx.c
  ${CMAKE_CURRENT_LIST_DIR}/src/gx_backend_null.c
  ${CMAKE_CURRENT_LIST_DIR}/src/hle.c
  ${CMAKE_CURRENT_LIST_DIR}/src/main.c
  ${CMAKE_CURRENT_LIST_DIR}/src/platform/platform_time.c)

if(GCRT_PLATFORM STREQUAL "sdl")
  list(APPEND GCRT_SOURCES ${CMAKE_CURRENT_LIST_DIR}/src/platform/platform_sdl.c)
elseif(GCRT_PLATFORM STREQUAL "android")
  list(APPEND GCRT_SOURCES ${CMAKE_CURRENT_LIST_DIR}/src/platform/platform_android.c)
else()
  list(APPEND GCRT_SOURCES ${CMAKE_CURRENT_LIST_DIR}/src/platform/platform_headless.c)
endif()

add_library(gcrt STATIC ${GCRT_SOURCES})
target_include_directories(gcrt PUBLIC ${CMAKE_CURRENT_LIST_DIR}/include PRIVATE ${CMAKE_CURRENT_LIST_DIR}/src)
set_target_properties(gcrt PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON POSITION_INDEPENDENT_CODE ON)
if(NOT MSVC)
  target_compile_options(gcrt PRIVATE -Wall -Wno-unused-function)
  target_compile_definitions(gcrt PRIVATE _FILE_OFFSET_BITS=64 _GNU_SOURCE)
endif()

find_package(Threads REQUIRED)
target_link_libraries(gcrt PUBLIC Threads::Threads)
if(NOT WIN32 AND NOT APPLE)
  target_link_libraries(gcrt PUBLIC m)
endif()

if(GCRT_PLATFORM STREQUAL "sdl")
  if(TARGET SDL2::SDL2)
    target_link_libraries(gcrt PUBLIC SDL2::SDL2)
  elseif(SDL2PC_FOUND)
    target_link_libraries(gcrt PUBLIC PkgConfig::SDL2PC)
  else()
    include(FetchContent)
    message(STATUS "SDL2 not found - fetching SDL2 2.30.9 source")
    FetchContent_Declare(SDL2 URL https://github.com/libsdl-org/SDL/releases/download/release-2.30.9/SDL2-2.30.9.tar.gz)
    set(SDL_SHARED OFF CACHE BOOL "" FORCE)
    set(SDL_STATIC ON CACHE BOOL "" FORCE)
    set(SDL_TEST OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(SDL2)
    target_link_libraries(gcrt PUBLIC SDL2::SDL2-static)
  endif()
elseif(GCRT_PLATFORM STREQUAL "android")
  target_compile_definitions(gcrt PUBLIC GCRT_NO_MAIN)
  target_link_libraries(gcrt PUBLIC android log)
endif()

# gcrt_add_game(<target> sources...)
function(gcrt_add_game target)
  if(GCRT_PLATFORM STREQUAL "android")
    add_library(${target} SHARED ${ARGN})
    # NativeActivity loads lib<name>.so; keep the entry point from being stripped.
    target_link_options(${target} PRIVATE "-u" "ANativeActivity_onCreate")
  else()
    add_executable(${target} ${ARGN})
  endif()
  target_link_libraries(${target} PRIVATE gcrt)
  set_target_properties(${target} PROPERTIES C_STANDARD 11)
  if(MSVC)
    target_compile_options(${target} PRIVATE /bigobj /wd4102 /wd4146)
  else()
    # Generated code: large functions, many labels. -O2 with no -fstrict-aliasing issues (memcpy based).
    target_compile_options(${target} PRIVATE -Wno-unused-label -Wno-unused-variable -fno-strict-aliasing)
  endif()
endfunction()
