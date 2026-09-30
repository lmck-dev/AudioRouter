/* Level tap: measures stereo audio and publishes it through a file, touching
 * nothing. PipeWire's filter-chain never refreshes a plugin's output control
 * values where anything outside can read them, so levels go into a ring of
 * blocks in $XDG_RUNTIME_DIR/audiorouter/meters/<host pid>.<slot>. */
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define PLUGIN_URI "urn:audiorouter:meter"
#define BLOCK 1024
#define RING 64
#define MAGIC 0x4d525241u /* "ARRM" */

typedef void *LV2_Handle;
typedef struct LV2_Descriptor {
    const char *URI;
    LV2_Handle (*instantiate)(const struct LV2_Descriptor *, double, const char *, const void *const *);
    void (*connect_port)(LV2_Handle, uint32_t, void *);
    void (*activate)(LV2_Handle);
    void (*run)(LV2_Handle, uint32_t);
    void (*deactivate)(LV2_Handle);
    void (*cleanup)(LV2_Handle);
    const void *(*extension_data)(const char *);
} LV2_Descriptor;

typedef struct { float peak_l, peak_r, ms_l, ms_r; } Entry;
typedef struct {
    uint32_t magic;
    uint32_t seq;       /* entries written so far; entry k is ring[k % RING] */
    Entry ring[RING];
} Shared;

enum { P_IN_L, P_IN_R, P_SLOT };

typedef struct {
    const float *in_l, *in_r, *slot;
    int opened_slot;
    char path[512];
    Shared *shared;
    uint32_t count;
    float peak_l, peak_r;
    double sum_l, sum_r;
} Plugin;

static LV2_Handle instantiate(const LV2_Descriptor *d, double rate, const char *b, const void *const *f)
{
    (void)d; (void)rate; (void)b; (void)f;
    Plugin *p = calloc(1, sizeof(Plugin));
    if (p)
        p->opened_slot = -1;
    return p;
}

static void connect_port(LV2_Handle h, uint32_t port, void *data)
{
    Plugin *p = h;
    switch (port) {
    case P_IN_L: p->in_l = data; break;
    case P_IN_R: p->in_r = data; break;
    case P_SLOT: p->slot = data; break;
    }
}

static void close_shared(Plugin *p)
{
    if (p->shared) {
        munmap(p->shared, sizeof(Shared));
        unlink(p->path);
        p->shared = NULL;
    }
}

/* Once per slot value, never per block: the only non-real-time work. */
static void open_shared(Plugin *p, int slot)
{
    close_shared(p);
    p->opened_slot = slot;
    const char *run = getenv("XDG_RUNTIME_DIR");
    if (!run)
        return;
    char dir[400];
    snprintf(dir, sizeof dir, "%s/audiorouter", run);
    mkdir(dir, 0700);
    snprintf(dir, sizeof dir, "%s/audiorouter/meters", run);
    mkdir(dir, 0700);
    snprintf(p->path, sizeof p->path, "%s/%d.%d", dir, (int)getpid(), slot);
    int fd = open(p->path, O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (fd < 0)
        return;
    if (ftruncate(fd, sizeof(Shared)) == 0) {
        void *m = mmap(NULL, sizeof(Shared), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (m != MAP_FAILED) {
            p->shared = m;
            p->shared->magic = MAGIC;
        }
    }
    close(fd);
}

static void run(LV2_Handle h, uint32_t n)
{
    Plugin *p = h;
    int slot = p->slot ? (int)lrintf(*p->slot) : 0;
    if (slot != p->opened_slot)
        open_shared(p, slot);
    if (!p->in_l || !p->in_r)
        return;
    for (uint32_t i = 0; i < n; i++) {
        float l = p->in_l[i], r = p->in_r[i];
        float al = fabsf(l), ar = fabsf(r);
        if (al > p->peak_l) p->peak_l = al;
        if (ar > p->peak_r) p->peak_r = ar;
        p->sum_l += (double)l * l;
        p->sum_r += (double)r * r;
        if (++p->count == BLOCK) {
            if (p->shared) {
                uint32_t seq = p->shared->seq;
                Entry *e = &p->shared->ring[seq % RING];
                e->peak_l = p->peak_l;
                e->peak_r = p->peak_r;
                e->ms_l = (float)(p->sum_l / BLOCK);
                e->ms_r = (float)(p->sum_r / BLOCK);
                __atomic_store_n(&p->shared->seq, seq + 1, __ATOMIC_RELEASE);
            }
            p->count = 0;
            p->peak_l = p->peak_r = 0.0f;
            p->sum_l = p->sum_r = 0.0;
        }
    }
}

static void cleanup(LV2_Handle h)
{
    close_shared(h);
    free(h);
}

static const void *extension_data(const char *uri) { (void)uri; return NULL; }

static const LV2_Descriptor descriptor = {
    PLUGIN_URI, instantiate, connect_port, NULL, run, NULL, cleanup, extension_data,
};

__attribute__((visibility("default")))
const LV2_Descriptor *lv2_descriptor(uint32_t index)
{
    return index == 0 ? &descriptor : NULL;
}
