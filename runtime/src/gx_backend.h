/*
 * Renderer backend interface for the GX front end.
 *
 * A backend receives decoded GX state and primitive batches - not raw FIFO
 * bytes - and translates them into host draw calls (OpenGL / Vulkan / D3D /
 * GLES).  The bundled "null" backend only records statistics; plugging a real
 * backend in is the main remaining piece for 3D output (see docs/ROADMAP.md).
 */
#ifndef GCRT_GX_BACKEND_H
#define GCRT_GX_BACKEND_H
#include <stdint.h>

typedef struct GXBackend {
    const char* name;
    void (*load_bp)(uint32_t reg, uint32_t value);
    void (*load_cp)(uint32_t reg, uint32_t value);
    void (*load_xf)(uint32_t addr, uint32_t count, const uint8_t* be_words);
    void (*load_xf_indexed)(uint8_t opcode, uint32_t value);
    /* primitive: 0x80 quads, 0x90 triangles, 0x98 strip, 0xA0 fan, 0xA8 lines,
     * 0xB0 line strip, 0xB8 points. Vertices are big-endian, `stride` bytes each. */
    void (*draw)(uint8_t primitive, int vat, uint32_t count, const uint8_t* vertices, uint32_t stride);
    /* return non-zero if the backend performed the copy itself */
    int (*efb_copy)(const uint32_t* bp_regs, uint32_t control);
} GXBackend;

typedef struct GXStats {
    uint64_t draws, vertices, bp_loads, display_lists, efb_copies, unknown_opcodes;
} GXStats;

extern GXStats gcrt_gx_stats;
extern const GXBackend* gcrt_gx_backend;

const GXBackend* gx_backend_null(void);
int gcrt_gx_vertex_size(int vat);
const uint32_t* gcrt_gx_cp_state(void);
const uint32_t* gcrt_gx_bp_state(void);
const uint32_t* gcrt_gx_xf_state(void);
#endif
