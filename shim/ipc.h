/*
 * The shared block between the bridge (our process) and the shim (inside the game).
 *
 * WHY A SHARED SECTION AND NOT SOMETHING RICHER
 *
 * The hot path is one float, rewritten ~100 times a second, and it must never block the
 * game's thread. A named section with a fixed layout costs a pointer dereference; a pipe or
 * socket would add a syscall and a failure mode per update, in someone else's process.
 *
 * IT CARRIES INPUT AS WELL AS OUTPUT, AND THAT IS NOT AN EXTRA
 *
 * Position reading is foreground-gated exactly like force output, so a background bridge
 * reads zeros while a game is in front -- vJoy's axes never move, the game cannot bind
 * steering, and a game that cannot bind steering never sends force feedback either. The
 * output half is useless without the input half.
 *
 * The shim is already inside the game, where reading works, so it publishes the whole reading
 * back through this same section and the bridge feeds vJoy from that.
 *
 * LAYOUT IS FROZEN AND MUST MATCH motor_sink.py EXACTLY.
 *
 *   off  type  field           written by
 *     0  u32   magic           both (on create)
 *     4  u32   version         both (on create)
 *     8  f32   force           bridge     -1.0 .. +1.0
 *    12  f32   gain            bridge      0.0 .. 1.0
 *    16  u64   bridge_tick_ms  bridge     heartbeat
 *    24  u64   shim_tick_ms    shim       heartbeat
 *    32  u32   shim_state      shim       see WH_STATE_*
 *    36  u32   has_reading     shim       is the reading below meaningful?
 *    40  f32   wheel           shim       -1.0 .. +1.0
 *    44  f32   throttle        shim        0.0 .. 1.0
 *    48  f32   brake           shim        0.0 .. 1.0
 *    52  f32   clutch          shim        0.0 .. 1.0
 *    56  f32   handbrake       shim        0.0 .. 1.0
 *    60  u32   buttons         shim       RacingWheelButtons bitfield
 *    64  i32   shifter_gear    shim       pattern shifter, -1 when absent
 *    68  u32   reserved
 *
 * Every field is naturally aligned at these offsets, so the C struct and Python's packed
 * '<IIffQQIIfffffIiI' describe the same 72 bytes. Do not reorder without changing both.
 *
 * Each side stamps its own heartbeat and reads the other's. That is what lets the shim tell
 * "the bridge wants zero force" from "the bridge died", which are the same float but must
 * behave differently: the first is obeyed, the second releases the motor.
 */

#ifndef WH33LH4X_IPC_H
#define WH33LH4X_IPC_H

#include <windows.h>

#define WH_IPC_NAME    L"Local\\WH33LH4X_bridge_v2"
#define WH_IPC_MAGIC   0x33334857u   /* 'WH33' little-endian */
#define WH_IPC_VERSION 2u

/* Older than this and the other side is considered gone. Generous next to a ~100 Hz update:
 * a game hitching for a quarter second must not drop the motor. */
#define WH_IPC_STALE_MS 500

#define WH_STATE_NONE   0u   /* no motor */
#define WH_STATE_MOTOR  1u   /* motor found, no effect loaded */
#define WH_STATE_ACTIVE 2u   /* effect loaded and running */

/* The reading the shim publishes, in the units the bridge already works in. */
typedef struct {
    float  wheel;        /* -1.0 .. +1.0 */
    float  throttle;     /*  0.0 .. 1.0  */
    float  brake;
    float  clutch;
    float  handbrake;
    UINT32 buttons;
    INT32  shifter_gear; /* -1 when the wheel has no pattern shifter */
} wh_reading;

#pragma pack(push, 1)
typedef struct {
    UINT32 magic;
    UINT32 version;
    float  force;
    float  gain;
    UINT64 bridge_tick_ms;
    UINT64 shim_tick_ms;
    UINT32 shim_state;
    UINT32 has_reading;
    float  wheel;
    float  throttle;
    float  brake;
    float  clutch;
    float  handbrake;
    UINT32 buttons;
    INT32  shifter_gear;
    UINT32 reserved;
} wh_shared;
#pragma pack(pop)

typedef struct {
    HANDLE     mapping;
    wh_shared *block;
} wh_ipc;

/* Create or attach to the section. FALSE means the log says why. */
BOOL wh_ipc_open(wh_ipc *ipc);
void wh_ipc_close(wh_ipc *ipc);

/* Has the bridge stamped its heartbeat recently enough to be considered alive? */
BOOL wh_ipc_bridge_alive(const wh_ipc *ipc);

/* Stamp our heartbeat and publish what the shim knows. `r` may be NULL when there is no
 * reading to report, which is not the same as a reading of all zeros. */
void wh_ipc_publish(wh_ipc *ipc, UINT32 state, const wh_reading *r);

#endif /* WH33LH4X_IPC_H */
