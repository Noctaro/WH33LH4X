/*
 * The GIP device layer -- a transcription of gip_protocol.py.
 *
 * Everything here was captured from Windows.Gaming.Input by gip_trace.py while it commanded
 * known magnitudes, and gip_protocol.py reproduces those bytes exactly. Nothing in this file
 * is invented; where a value is not understood it is replayed, and said so.
 *
 * WHAT IS VERIFIED: the force parameter. Param 0x0008 of a type 0x0b message is an IEEE-754
 * float32 in -1..+1, and it round-tripped across +0.00 / +0.25 / +0.50 / +1.00 / -0.50.
 *
 * WHAT IS REPLAYED WITHOUT BEING UNDERSTOOD: the 186-byte table below, and most of the
 * 256-slot parameter bank. They are what the firmware saw when it worked.
 */

#include "gip.h"
#include "shim_log.h"

#include <string.h>

#define GIP_PATH L"\\\\.\\XboxGIP"

/*
 * GIP_ADD_REENUMERATE_CALLER_CONTEXT, from the GIP-on-Windows writeup:
 * CTL_CODE(0x4000, 0x734, METHOD_BUFFERED, FILE_ANY_ACCESS).
 *
 * Without this the driver never re-announces devices to a freshly opened handle, so no type
 * 0x02 arrives and the device id cannot be learned. Measured from Python: twelve reads at
 * twelve buffer sizes all sat pending with no error, because nothing was ever queued for us.
 */
#define GIP_REENUMERATE ((0x4000 << 16) | (0x734 << 2))

#define GIP_HEADER_BYTES  20
#define PARAM_SLOTS       10
#define PARAM_BLOCK_BYTES (PARAM_SLOTS * 6)   /* always 60, even for one real parameter */
#define PARAM_BANK_SIZE   0x100
#define PARAM_PADDING_ID  0xFFFF
#define PARAM_X_FORCE     0x0008

#define TYPE_SHORT_CMD    0x0A
#define TYPE_PARAM_BLOCK  0x0B
#define TYPE_STATE        0x0C
#define TYPE_TABLE        0x0D
#define TYPE_ANNOUNCE     0x02

#define HORI_VID 0x0F0D
#define HORI_PID 0x015C

#define TABLE_CHUNK 48
#define READ_BUFFER 4096

/* The effect table, uploaded in four 0x0d chunks before the parameter bank means anything.
 * Captured from WGI loading ONE effect: a ConstantForceEffect on X. That is no limitation --
 * ffb_render.py already reduces every game effect to a single constant force rewritten at
 * loop rate, so this is the only effect the bridge ever needs. */
static const BYTE FFB_TABLE[186] = {
    0xa0, 0x00, 0x50, 0x03, 0x4a, 0x01, 0xd0, 0x4b, 0x40, 0xd0, 0x4b, 0x24,
    0x11, 0x24, 0x40, 0x24, 0x11, 0x24, 0x14, 0x00, 0x24, 0x44, 0x24, 0x21,
    0x06, 0x21, 0xa0, 0x24, 0x20, 0x40, 0x14, 0x01, 0x00, 0x22, 0x02, 0x22,
    0x01, 0x14, 0x20, 0x14, 0x01, 0x20, 0xa0, 0x00, 0x21, 0x42, 0x14, 0x01,
    0x02, 0x22, 0x02, 0x22, 0x01, 0xa0, 0x01, 0x21, 0x03, 0x14, 0x02, 0x01,
    0xa0, 0x01, 0x21, 0x03, 0x21, 0x10, 0x04, 0x13, 0x24, 0x14, 0x01, 0x23,
    0xd0, 0x14, 0x02, 0x22, 0x41, 0x23, 0x22, 0x21, 0x09, 0x21, 0xa0, 0x02,
    0x21, 0x45, 0x14, 0x03, 0x25, 0x11, 0x14, 0x01, 0x14, 0x03, 0x22, 0x11,
    0x20, 0x22, 0x23, 0x45, 0x23, 0x22, 0x30, 0x25, 0x22, 0x25, 0x13, 0x23,
    0x14, 0x03, 0x23, 0x11, 0x14, 0x04, 0x14, 0x05, 0x22, 0x12, 0x23, 0x22,
    0x23, 0x12, 0x23, 0x25, 0x23, 0x10, 0x14, 0x05, 0x23, 0x26, 0x45, 0x14,
    0x06, 0x25, 0x11, 0x14, 0x06, 0x20, 0x22, 0x45, 0x22, 0x23, 0x30, 0x25,
    0x23, 0x25, 0x13, 0x22, 0x14, 0x06, 0x23, 0x11, 0x14, 0x07, 0x14, 0x05,
    0x22, 0x12, 0x23, 0x22, 0x23, 0x12, 0x23, 0x25, 0x23, 0x10, 0x26, 0x23,
    0x22, 0x12, 0x21, 0x22, 0x21, 0x12, 0x14, 0x08, 0x21, 0x22, 0x12, 0x14,
    0x09, 0x22, 0x21, 0xa0, 0x21, 0x53,
};

/* The 9-byte type 0x0c "state" bodies: 0x00 clears, 0xf0 runs. */
static const BYTE STATE_CLEAR[9]   = { 0x00, 0, 0, 0, 0, 0, 0, 0, 0 };
static const BYTE STATE_RUNNING[9] = { 0xf0, 0, 0, 0, 0, 0, 0, 0, 0 };
/* Type 0x0a: 0x00 resets, 0x06 starts and keeps alive. */
static const BYTE CMD_RESET[3]     = { 0x00, 0x00, 0x00 };
static const BYTE SHORT_CMD[3]     = { 0x06, 0x00, 0x00 };

/* ------------------------------------------------------------------ encoding */

static void put_u16(BYTE *p, WORD v)
{
    p[0] = (BYTE)(v & 0xFF);
    p[1] = (BYTE)(v >> 8);
}

static void put_u32(BYTE *p, DWORD v)
{
    p[0] = (BYTE)(v & 0xFF);
    p[1] = (BYTE)((v >> 8) & 0xFF);
    p[2] = (BYTE)((v >> 16) & 0xFF);
    p[3] = (BYTE)((v >> 24) & 0xFF);
}

static WORD get_u16(const BYTE *p)
{
    return (WORD)(p[0] | (p[1] << 8));
}

/*
 * Frame one message. Sequence is always 0 because every captured write used 0 -- 353 writes
 * deduplicated to three distinct byte strings per magnitude, which could not happen if the
 * field were counting.
 */
static DWORD gip_frame(const gip_device *dev, BYTE type, const BYTE *body, DWORD body_len,
                       BYTE *out, DWORD out_cap)
{
    if (out_cap < GIP_HEADER_BYTES + body_len)
        return 0;
    memcpy(out, dev->device_id, 8);
    out[8]  = type;
    out[9]  = 0;              /* flags: 0x20 marks driver announcements, never ours */
    put_u16(out + 10, 0);     /* sequence */
    put_u32(out + 12, body_len);
    put_u32(out + 16, 0);     /* reserved */
    if (body_len)
        memcpy(out + GIP_HEADER_BYTES, body, body_len);
    return GIP_HEADER_BYTES + body_len;
}

/* One overlapped write, waited to completion. */
static BOOL gip_write(gip_device *dev, const BYTE *buf, DWORD len)
{
    OVERLAPPED ov;
    DWORD      written = 0;
    BOOL       ok;

    memset(&ov, 0, sizeof(ov));
    ov.hEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!ov.hEvent)
        return FALSE;

    ok = WriteFile(dev->handle, buf, len, &written, &ov);
    if (!ok && GetLastError() == ERROR_IO_PENDING)
        ok = GetOverlappedResult(dev->handle, &ov, &written, TRUE);

    if (!ok) {
        dev->last_error = GetLastError();
        dev->write_errors++;
    } else {
        dev->writes++;
    }
    CloseHandle(ov.hEvent);
    return ok;
}

/*
 * Append one message to the stream dump, hex per line, matching gip_direct.py --dump.
 *
 * This exists because "the C is a transcription of the verified Python" is an assumption, not
 * evidence -- and hand transcription is exactly where errors hide. `gip_diff.py` can compare
 * this against a WGI capture, which is the only way to know the bytes leaving the shim are the
 * bytes we think they are.
 */
static void gip_dump(gip_device *dev, const BYTE *msg, DWORD n)
{
    char   line[3 * (GIP_HEADER_BYTES + 256) + 4];
    DWORD  i, written = 0;
    HANDLE f;

    if (!dev->dump_path[0])
        return;
    f = CreateFileW(dev->dump_path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE)
        return;
    for (i = 0; i < n && (i * 2 + 3) < sizeof(line); i++) {
        static const char HEX[] = "0123456789abcdef";
        line[i * 2]     = HEX[msg[i] >> 4];
        line[i * 2 + 1] = HEX[msg[i] & 0x0F];
    }
    line[i * 2]     = '\r';
    line[i * 2 + 1] = '\n';
    WriteFile(f, line, i * 2 + 2, &written, NULL);
    CloseHandle(f);
}

static BOOL gip_send(gip_device *dev, BYTE type, const BYTE *body, DWORD body_len)
{
    BYTE  msg[GIP_HEADER_BYTES + 256];
    DWORD n = gip_frame(dev, type, body, body_len, msg, sizeof(msg));
    if (!n)
        return FALSE;
    gip_dump(dev, msg, n);
    return gip_write(dev, msg, n);
}

/* Build a 60-byte parameter block. `count` real slots, the rest padded with 0xffff/0. */
static void build_param_block(BYTE *body, const WORD *ids, const BYTE (*values)[4], int count)
{
    int i;
    for (i = 0; i < PARAM_SLOTS; i++) {
        BYTE *slot = body + i * 6;
        if (i < count) {
            put_u16(slot, ids[i]);
            memcpy(slot + 2, values[i], 4);
        } else {
            put_u16(slot, PARAM_PADDING_ID);
            memset(slot + 2, 0, 4);
        }
    }
}

/* ------------------------------------------------------------------ open */

BOOL gip_open(gip_device *dev)
{
    DWORD returned = 0;

    memset(dev, 0, sizeof(*dev));
    dev->handle = CreateFileW(GIP_PATH, GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING,
                              FILE_FLAG_OVERLAPPED, NULL);
    if (dev->handle == INVALID_HANDLE_VALUE) {
        dev->handle = NULL;
        dev->last_error = GetLastError();
        shim_log("gip: CreateFileW(XboxGIP) failed, err=%lu", dev->last_error);
        return FALSE;
    }
    shim_log("gip: opened XboxGIP");

    /* Not fatal: the announce may already be queued, or an id may come from config. */
    if (DeviceIoControl(dev->handle, GIP_REENUMERATE, NULL, 0, NULL, 0, &returned, NULL))
        shim_log("gip: reenumerate IOCTL accepted");
    else
        shim_log("gip: reenumerate IOCTL refused, err=%lu", GetLastError());

    return TRUE;
}

void gip_close(gip_device *dev)
{
    if (dev->handle) {
        CancelIoEx(dev->handle, NULL);
        CloseHandle(dev->handle);
        dev->handle = NULL;
    }
}

/* ------------------------------------------------------------------ discovery */

BOOL gip_discover(gip_device *dev, DWORD timeout_ms)
{
    BYTE       buf[READ_BUFFER];
    OVERLAPPED ov;
    HANDLE     ev;
    DWORD      deadline = GetTickCount() + timeout_ms;
    DWORD      seen = 0;

    ev = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!ev)
        return FALSE;

    while (GetTickCount() < deadline) {
        DWORD got = 0;
        BOOL  ok;

        memset(&ov, 0, sizeof(ov));
        ResetEvent(ev);
        ov.hEvent = ev;

        ok = ReadFile(dev->handle, buf, sizeof(buf), &got, &ov);
        if (!ok && GetLastError() == ERROR_IO_PENDING) {
            if (WaitForSingleObject(ev, 250) == WAIT_TIMEOUT) {
                /* Leaving a read in flight would complete into a stack buffer we are
                 * about to reuse, so cancel and drain before looping. */
                CancelIoEx(dev->handle, &ov);
                GetOverlappedResult(dev->handle, &ov, &got, TRUE);
                continue;
            }
            ok = GetOverlappedResult(dev->handle, &ov, &got, FALSE);
        }
        if (!ok || got < GIP_HEADER_BYTES)
            continue;

        seen++;
        /* Announce body: [8-byte device id][u16 VID][u16 PID][...]. Matching VID/PID is what
         * makes this specific to the wheel rather than to enumeration order. */
        if (buf[8] == TYPE_ANNOUNCE && got >= GIP_HEADER_BYTES + 12) {
            const BYTE *body = buf + GIP_HEADER_BYTES;
            WORD vid = get_u16(body + 8);
            WORD pid = get_u16(body + 10);
            if (vid == HORI_VID && pid == HORI_PID) {
                memcpy(dev->device_id, body, 8);
                dev->have_device_id = TRUE;
                shim_log("gip: learned device id %02x%02x%02x%02x%02x%02x%02x%02x"
                         " from announce (VID %04X PID %04X)",
                         body[0], body[1], body[2], body[3],
                         body[4], body[5], body[6], body[7], vid, pid);
                CloseHandle(ev);
                return TRUE;
            }
        }
    }

    CloseHandle(ev);
    shim_log("gip: no matching announce in %lu ms (%lu message(s) seen)", timeout_ms, seen);
    return FALSE;
}

BOOL gip_set_device_id_hex(gip_device *dev, const char *hex)
{
    int i;
    for (i = 0; i < 8; i++) {
        int hi, lo;
        char c = hex[i * 2], d = hex[i * 2 + 1];
        hi = (c >= '0' && c <= '9') ? c - '0'
           : (c >= 'a' && c <= 'f') ? c - 'a' + 10
           : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
        lo = (d >= '0' && d <= '9') ? d - '0'
           : (d >= 'a' && d <= 'f') ? d - 'a' + 10
           : (d >= 'A' && d <= 'F') ? d - 'A' + 10 : -1;
        if (hi < 0 || lo < 0) {
            shim_log("gip: device id in config is not 16 hex characters");
            return FALSE;
        }
        dev->device_id[i] = (BYTE)((hi << 4) | lo);
    }
    dev->have_device_id = TRUE;
    shim_log("gip: using configured device id %s", hex);
    return TRUE;
}

/* ------------------------------------------------------------------ the effect */

/*
 * Arm the effect, in WGI's exact order.
 *
 * THIS ORDER IS LOAD-BEARING AND WAS GOT WRONG ONCE. The first version uploaded the table,
 * then wrote all 256 parameter slots with values, then set state -- and the wheel went slack
 * without ever producing torque. The firmware took the motor and then had no effect to run,
 * which also left it dead to Windows.Gaming.Input until the wheel was replugged.
 *
 * Diffing the ordered byte streams showed WGI resets FIRST, zeroes the whole bank, clears
 * state, uploads the table, and only then writes ONE block of values. Steps 1-3 were missing
 * entirely and step 5 was 26 blocks instead of one.
 */
BOOL gip_load_effect(gip_device *dev)
{
    BYTE  body[PARAM_BLOCK_BYTES];
    BYTE  chunk[5 + TABLE_CHUNK];
    DWORD offset;
    int   start;

    if (!dev->have_device_id)
        return FALSE;

    /* 1-3: reset, then zero every slot in the bank, then clear state again. */
    if (!gip_send(dev, TYPE_SHORT_CMD, CMD_RESET, sizeof(CMD_RESET)) ||
        !gip_send(dev, TYPE_STATE, STATE_CLEAR, sizeof(STATE_CLEAR))) {
        shim_log("gip: reset failed, err=%lu", dev->last_error);
        return FALSE;
    }
    for (start = 0; start < PARAM_BANK_SIZE; start += PARAM_SLOTS) {
        WORD ids[PARAM_SLOTS];
        BYTE values[PARAM_SLOTS][4];
        int  i;
        int  count = PARAM_BANK_SIZE - start;
        if (count > PARAM_SLOTS)
            count = PARAM_SLOTS;
        for (i = 0; i < count; i++) {
            ids[i] = (WORD)(start + i);
            memset(values[i], 0, 4);
        }
        build_param_block(body, ids, values, count);
        if (!gip_send(dev, TYPE_PARAM_BLOCK, body, PARAM_BLOCK_BYTES)) {
            shim_log("gip: zeroing block at 0x%04x failed, err=%lu", start, dev->last_error);
            return FALSE;
        }
    }
    if (!gip_send(dev, TYPE_STATE, STATE_CLEAR, sizeof(STATE_CLEAR))) {
        shim_log("gip: clear before table failed, err=%lu", dev->last_error);
        return FALSE;
    }

    /* 4: table upload -- [u8 0][u16 total][u16 offset][48 bytes], zero-padded to a chunk. */
    for (offset = 0; offset < sizeof(FFB_TABLE); offset += TABLE_CHUNK) {
        DWORD remaining = (DWORD)sizeof(FFB_TABLE) - offset;
        DWORD take = remaining < TABLE_CHUNK ? remaining : TABLE_CHUNK;
        memset(chunk, 0, sizeof(chunk));
        chunk[0] = 0;
        put_u16(chunk + 1, (WORD)sizeof(FFB_TABLE));
        put_u16(chunk + 3, (WORD)offset);
        memcpy(chunk + 5, FFB_TABLE + offset, take);
        if (!gip_send(dev, TYPE_TABLE, chunk, sizeof(chunk))) {
            shim_log("gip: table chunk at %lu failed, err=%lu", offset, dev->last_error);
            return FALSE;
        }
    }

    /*
     * 5: ONE block of real values, ids 0x0000..0x0009. Only six are non-zero, and they are
     * replayed as captured rather than understood:
     *   0x0001 = 600000.0  (consistent with the commanded 60 s at 100 us/unit -- one sample)
     *   0x0002 = integer 1
     *   0x0003 = -1.0   0x0005 = +1.0   0x0006 = -1.0   0x0009 = +1.0
     */
    {
        WORD ids[PARAM_SLOTS];
        BYTE values[PARAM_SLOTS][4];
        int  i;
        for (i = 0; i < PARAM_SLOTS; i++) {
            float f;
            ids[i] = (WORD)i;
            switch (i) {
            case 0x0001: f = 600000.0f; memcpy(values[i], &f, 4); break;
            case 0x0002: put_u32(values[i], 1);                   break;
            case 0x0003: f = -1.0f;     memcpy(values[i], &f, 4); break;
            case 0x0005: f = 1.0f;      memcpy(values[i], &f, 4); break;
            case 0x0006: f = -1.0f;     memcpy(values[i], &f, 4); break;
            case 0x0009: f = 1.0f;      memcpy(values[i], &f, 4); break;
            default:     memset(values[i], 0, 4);                 break;
            }
        }
        build_param_block(body, ids, values, PARAM_SLOTS);
        if (!gip_send(dev, TYPE_PARAM_BLOCK, body, PARAM_BLOCK_BYTES)) {
            shim_log("gip: value block failed, err=%lu", dev->last_error);
            return FALSE;
        }
    }

    /* 6: run, then start. */
    if (!gip_send(dev, TYPE_STATE, STATE_RUNNING, sizeof(STATE_RUNNING)) ||
        !gip_send(dev, TYPE_SHORT_CMD, SHORT_CMD, sizeof(SHORT_CMD))) {
        shim_log("gip: effect start failed, err=%lu", dev->last_error);
        return FALSE;
    }

    dev->loaded = TRUE;
    shim_log("gip: effect loaded (%lu writes, %lu error(s))", dev->writes, dev->write_errors);
    return TRUE;
}

BOOL gip_set_force(gip_device *dev, float magnitude)
{
    BYTE body[PARAM_BLOCK_BYTES];
    WORD ids[1];
    BYTE values[1][4];

    if (!dev->loaded)
        return FALSE;
    if (magnitude > 1.0f)
        magnitude = 1.0f;
    if (magnitude < -1.0f)
        magnitude = -1.0f;

    ids[0] = PARAM_X_FORCE;
    memcpy(values[0], &magnitude, 4);
    build_param_block(body, ids, values, 1);
    return gip_send(dev, TYPE_PARAM_BLOCK, body, PARAM_BLOCK_BYTES);
}

BOOL gip_pump(gip_device *dev)
{
    if (!dev->loaded)
        return FALSE;
    return gip_send(dev, TYPE_STATE, STATE_RUNNING, sizeof(STATE_RUNNING));
}

BOOL gip_keepalive(gip_device *dev)
{
    if (!dev->loaded)
        return FALSE;
    return gip_send(dev, TYPE_SHORT_CMD, SHORT_CMD, sizeof(SHORT_CMD));
}
