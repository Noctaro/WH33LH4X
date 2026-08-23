/*
 * WH33LH4X shim -- driving the wheel through Windows.Gaming.Input, from inside the game.
 *
 * WHY THIS AND NOT THE GIP DRIVER
 *
 * The obvious shortcut -- replay the packets WGI sends to \\.\XboxGIP -- is a DEAD END and was
 * measured as one. The arming sequence was reproduced byte-for-byte, verified programmatically
 * against a capture, sent from inside a focused game with zero write errors, and the motor
 * never moved. Identical bytes, different result: the driver distinguishes clients by something
 * other than what they send. See .claude/memory/gip-force-packet-format.md. Do not retry it.
 *
 * WGI itself does drive this motor -- reliably, every time -- but only for a process that is in
 * the foreground. That is the entire reason this code lives in a dinput8.dll proxy: the game is
 * foreground, so WGI works here.
 *
 * WHY THIS IS NOT HUNDREDS OF LINES OF HAND-WRITTEN ABI
 *
 * The Windows SDK ships MIDL-generated headers with C vtable structs for these interfaces, and
 * they compile under zig's clang. So this is ordinary COM: QueryInterface, vtable calls, and
 * release. Only the IIDs are declared-but-not-defined for MinGW, and those come from the .idl
 * files next to the headers.
 */

#ifndef WH33LH4X_WGI_H
#define WH33LH4X_WGI_H

#include <windows.h>

typedef struct wgi_motor wgi_motor;

/*
 * Find the wheel and take its force-feedback motor.
 *
 * Polls, because devices arrive asynchronously and the fastest one wins -- never accept the
 * first non-empty enumeration. Returns NULL and logs the reason on failure.
 */
wgi_motor *wgi_open(DWORD timeout_ms);

/* Load and start a constant-force effect held open for the life of the session. */
BOOL wgi_load_effect(wgi_motor *m, float gain);

/* Rewrite the held effect's magnitude, -1.0 .. +1.0. Clamped. Cheap enough for ~100 Hz. */
BOOL wgi_set_force(wgi_motor *m, float magnitude);

/*
 * Read the wheel's current position and pedals.
 *
 * Reading is foreground-gated just like output, which is why it has to happen HERE rather than
 * in the bridge: a background process reads zeros while a game is in front, vJoy's axes never
 * move, and a game that cannot bind steering never sends force feedback either.
 *
 * `wheel` is -1..+1, pedals 0..1. FALSE means no reading was available.
 */
BOOL wgi_read(wgi_motor *m, float *wheel, float *throttle, float *brake,
              float *clutch, float *handbrake, UINT32 *buttons, INT32 *shifter_gear);

/*
 * Give the effect back but keep the motor handle, so a later wgi_load_effect can start again.
 *
 * Used when the bridge goes quiet: holding a loaded effect that nothing is driving is the
 * state that leaves the wheel dead for every other application.
 */
void wgi_release_effect(wgi_motor *m);

/* Stop, unload, reset, release. Resetting matters: a released motor can otherwise be left
 * accepting effects while producing no torque. */
void wgi_close(wgi_motor *m);

/* For the W1 spike: what did we find? Written into the shim log by wgi_open. */
BOOL wgi_have_motor(const wgi_motor *m);

#endif /* WH33LH4X_WGI_H */
