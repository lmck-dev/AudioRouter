/* Noise suppression for voice: an LV2 plugin around the system's librnnoise.
 *
 * Written because the packaged werman plugin misbehaves inside PipeWire's
 * filter-chain (half level, late, distorted) and keeps its settings on an atom
 * port filter-chain cannot drive. This one has ordinary control ports only.
 *
 * RNNoise works on 480-sample frames at 48 kHz. Samples are gathered into a
 * frame and the output plays the previous processed frame at the same
 * position, so any block size works. RNNoise itself delays speech by a further
 * 960 samples (measured against the dry signal), so the dry side of the mix is
 * delayed to match - otherwise a partial mix comb-filters.
 *
 * Built with no LV2 or RNNoise headers installed: both ABIs are declared here.
 */

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PLUGIN_URI "urn:audiorouter:rnnoise"
#define FRAME 480
#define FRAME_MS 10.0f
#define RNN_DELAY 960
#define LATENCY (FRAME + RNN_DELAY)

/* --- librnnoise ------------------------------------------------------- */
typedef struct DenoiseState DenoiseState;
DenoiseState *rnnoise_create(void *model);
void rnnoise_destroy(DenoiseState *st);
float rnnoise_process_frame(DenoiseState *st, float *out, const float *in);

/* --- LV2 core --------------------------------------------------------- */
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

enum { P_IN, P_OUT, P_THRESHOLD, P_HOLD, P_MIX, P_ENABLED, P_LATENCY };

typedef struct {
    const float *in;
    float *out;
    const float *threshold; /* % voice probability below which it mutes; 0 = never */
    const float *hold;      /* ms the gate stays open after the last voiced frame */
    const float *mix;       /* % of processed signal */
    const float *enabled;
    float *latency;

    int passthrough;        /* not 48 kHz: RNNoise cannot run */
    DenoiseState *st;
    float frame_in[FRAME];  /* gathering, scaled to 16-bit range */
    float dry[LATENCY];     /* input delay line, so dry lines up with wet */
    uint32_t dry_pos;
    float wet[FRAME];       /* the frame now playing, processed */
    float processed[FRAME];
    uint32_t pos;
    float gain;             /* gate gain at the end of the last frame */
    uint32_t hold_left;     /* frames */
} Plugin;

static LV2_Handle instantiate(const LV2_Descriptor *d, double rate, const char *path, const void *const *f)
{
    (void)d; (void)path; (void)f;
    Plugin *p = calloc(1, sizeof(Plugin));
    if (!p)
        return NULL;
    p->passthrough = fabs(rate - 48000.0) > 0.5;
    if (!p->passthrough) {
        p->st = rnnoise_create(NULL);
        if (!p->st) {
            free(p);
            return NULL;
        }
    }
    p->gain = 1.0f;
    return p;
}

static void connect_port(LV2_Handle h, uint32_t port, void *data)
{
    Plugin *p = h;
    switch (port) {
    case P_IN: p->in = data; break;
    case P_OUT: p->out = data; break;
    case P_THRESHOLD: p->threshold = data; break;
    case P_HOLD: p->hold = data; break;
    case P_MIX: p->mix = data; break;
    case P_ENABLED: p->enabled = data; break;
    case P_LATENCY: p->latency = data; break;
    }
}

static void activate(LV2_Handle h)
{
    Plugin *p = h;
    memset(p->frame_in, 0, sizeof p->frame_in);
    memset(p->dry, 0, sizeof p->dry);
    p->dry_pos = 0;
    memset(p->wet, 0, sizeof p->wet);
    p->pos = 0;
    p->gain = 1.0f;
    p->hold_left = 0;
}

static float ctl(const float *port, float fallback)
{
    return port ? *port : fallback;
}

/* Denoise the gathered frame into p->wet, fading the gate across the frame. */
static void process_frame(Plugin *p)
{
    float vad = rnnoise_process_frame(p->st, p->processed, p->frame_in);

    float threshold = ctl(p->threshold, 0.0f) / 100.0f;
    float hold_ms = ctl(p->hold, 200.0f);
    float target = 1.0f;
    if (threshold > 0.0f) {
        if (vad >= threshold)
            p->hold_left = (uint32_t)(hold_ms / FRAME_MS + 0.5f);
        else if (p->hold_left > 0)
            p->hold_left--;
        target = (vad >= threshold || p->hold_left > 0) ? 1.0f : 0.0f;
    }

    float step = (target - p->gain) / FRAME;
    for (int i = 0; i < FRAME; i++) {
        p->gain += step;
        p->wet[i] = p->processed[i] * p->gain / 32768.0f;
    }
    p->gain = target;
}

static void run(LV2_Handle h, uint32_t n)
{
    Plugin *p = h;
    if (p->latency)
        *p->latency = p->passthrough ? 0.0f : (float)LATENCY;
    if (!p->in || !p->out)
        return;
    if (p->passthrough) {
        if (p->out != p->in)
            memmove(p->out, p->in, n * sizeof(float));
        return;
    }

    float mix = ctl(p->mix, 100.0f) / 100.0f;
    if (mix < 0.0f) mix = 0.0f;
    if (mix > 1.0f) mix = 1.0f;
    if (ctl(p->enabled, 1.0f) < 0.5f)
        mix = 0.0f; /* still delayed by the full latency, so switching is seamless */

    for (uint32_t i = 0; i < n; i++) {
        float x = p->in[i]; /* read before writing: in and out may be one buffer */
        float y = p->wet[p->pos] * mix + p->dry[p->dry_pos] * (1.0f - mix);
        p->dry[p->dry_pos] = x;
        if (++p->dry_pos == LATENCY)
            p->dry_pos = 0;
        p->frame_in[p->pos] = x * 32768.0f;
        p->out[i] = y;
        if (++p->pos == FRAME) {
            process_frame(p);
            p->pos = 0;
        }
    }
}

static void deactivate(LV2_Handle h)
{
    (void)h;
}

static void cleanup(LV2_Handle h)
{
    Plugin *p = h;
    if (p->st)
        rnnoise_destroy(p->st);
    free(p);
}

static const void *extension_data(const char *uri)
{
    (void)uri;
    return NULL;
}

static const LV2_Descriptor descriptor = {
    PLUGIN_URI, instantiate, connect_port, activate, run, deactivate, cleanup, extension_data,
};

__attribute__((visibility("default")))
const LV2_Descriptor *lv2_descriptor(uint32_t index)
{
    return index == 0 ? &descriptor : NULL;
}
