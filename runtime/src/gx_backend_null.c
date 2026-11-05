/* Statistics-only renderer backend (default until a GPU backend is attached). */
#include "gx_backend.h"
#include <stddef.h>

static const GXBackend g_null = { "null", NULL, NULL, NULL, NULL, NULL, NULL };

const GXBackend* gx_backend_null(void) { return &g_null; }
