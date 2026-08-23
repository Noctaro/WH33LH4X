/*
 * WH33LH4X shim -- talking to the GIP driver from inside the game's process.
 *
 * This is the C side of `gip_protocol.py`. That module was built by capturing what
 * Windows.Gaming.Input sends to `\\.\XboxGIP` while commanding known forces, and it
 * reproduces those bytes exactly; this header exposes the same protocol to the shim.
 *
 * WHY IT LIVES IN THE SHIM AT ALL: force output is gated on the calling process being in the
 * foreground. A background bridge cannot drive this motor while a game is in front, so the
 * output stage has to run inside the game -- which is what the dinput8.dll proxy achieves.
 */

#ifndef WH33LH4X_GIP_H
#define WH33LH4X_GIP_H

#include <windows.h>

/* An 8-byte id the driver assigns per device. Learned, never hard-coded. */
typedef struct {
    HANDLE handle;
    BYTE   device_id[8];
    BOOL   have_device_id;
    BOOL   loaded;         /* the effect table + parameter bank have been uploaded */
    DWORD  writes;
    DWORD  write_errors;
    DWORD  last_error;
} gip_device;

/* All of these log through the shim's own log; none of them throw or abort. */

/* Open \\.\XboxGIP and issue the re-enumerate IOCTL. FALSE means the log says why. */
BOOL gip_open(gip_device *dev);

/*
 * Listen for a type 0x02 announce matching the wheel's VID/PID and take the id from it.
 * `timeout_ms` bounds the wait. FALSE means no announce arrived -- the caller may then supply
 * an id from configuration rather than giving up.
 */
BOOL gip_discover(gip_device *dev, DWORD timeout_ms);

/* Use a known id instead of discovering one. `hex` is 16 hex characters. */
BOOL gip_set_device_id_hex(gip_device *dev, const char *hex);

/* Upload the captured constant-force effect. Must succeed before gip_set_force does anything. */
BOOL gip_load_effect(gip_device *dev);

/* Command X-axis force, -1.0 .. +1.0. Clamped. */
BOOL gip_set_force(gip_device *dev, float magnitude);

void gip_close(gip_device *dev);

#endif /* WH33LH4X_GIP_H */
