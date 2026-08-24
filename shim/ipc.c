/*
 * The shared section, from the shim's side. See ipc.h for the layout and why it is frozen.
 */

#include "ipc.h"
#include "shim_log.h"

#include <string.h>

BOOL wh_ipc_open(wh_ipc *ipc)
{
    BOOL created;

    memset(ipc, 0, sizeof(*ipc));

    /*
     * CreateFileMapping on a name that already exists attaches to it and reports
     * ERROR_ALREADY_EXISTS, so neither side needs to start first and there is no race to lose.
     * Whoever gets there first initialises the header; the other must NOT, or it would wipe a
     * heartbeat that is already running.
     */
    ipc->mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
                                      0, sizeof(wh_shared), WH_IPC_NAME);
    if (!ipc->mapping) {
        shim_log("ipc: CreateFileMapping failed, err=%lu", GetLastError());
        return FALSE;
    }
    created = (GetLastError() != ERROR_ALREADY_EXISTS);

    ipc->block = (wh_shared *)MapViewOfFile(ipc->mapping, FILE_MAP_ALL_ACCESS, 0, 0,
                                            sizeof(wh_shared));
    if (!ipc->block) {
        shim_log("ipc: MapViewOfFile failed, err=%lu", GetLastError());
        CloseHandle(ipc->mapping);
        ipc->mapping = NULL;
        return FALSE;
    }

    if (created) {
        memset(ipc->block, 0, sizeof(*ipc->block));
        ipc->block->magic = WH_IPC_MAGIC;
        ipc->block->version = WH_IPC_VERSION;
        ipc->block->gain = 1.0f;
    } else if (ipc->block->magic != WH_IPC_MAGIC ||
               ipc->block->version != WH_IPC_VERSION) {
        /* A stale section from an older build would be read as garbage floats and turned
         * straight into torque. Refuse rather than guess. */
        shim_log("ipc: section has magic 0x%08lx version %lu, expected 0x%08lx version %lu"
                 " -- refusing to use it",
                 (unsigned long)ipc->block->magic, (unsigned long)ipc->block->version,
                 (unsigned long)WH_IPC_MAGIC, (unsigned long)WH_IPC_VERSION);
        wh_ipc_close(ipc);
        return FALSE;
    }

    shim_log("ipc: %s %ls", created ? "created" : "attached to", WH_IPC_NAME);
    return TRUE;
}

void wh_ipc_close(wh_ipc *ipc)
{
    if (ipc->block) {
        UnmapViewOfFile(ipc->block);
        ipc->block = NULL;
    }
    if (ipc->mapping) {
        CloseHandle(ipc->mapping);
        ipc->mapping = NULL;
    }
}

BOOL wh_ipc_bridge_alive(const wh_ipc *ipc)
{
    UINT64 now, stamp;

    if (!ipc->block)
        return FALSE;
    stamp = ipc->block->bridge_tick_ms;
    if (!stamp)
        return FALSE;
    now = GetTickCount64();
    /* Unsigned arithmetic, so a bridge stamp from the future (clock skew, or a stale section)
     * wraps to something enormous and reads as dead rather than as permanently alive. */
    return (now - stamp) < WH_IPC_STALE_MS;
}

UINT64 wh_ipc_bridge_staleness_ms(const wh_ipc *ipc)
{
    UINT64 stamp;

    if (!ipc->block)
        return 0;
    stamp = ipc->block->bridge_tick_ms;
    if (!stamp)
        return 0;
    return GetTickCount64() - stamp;
}

void wh_ipc_publish(wh_ipc *ipc, UINT32 state, const wh_reading *r)
{
    if (!ipc->block)
        return;
    ipc->block->shim_state = state;
    if (r) {
        ipc->block->wheel = r->wheel;
        ipc->block->throttle = r->throttle;
        ipc->block->brake = r->brake;
        ipc->block->clutch = r->clutch;
        ipc->block->handbrake = r->handbrake;
        ipc->block->buttons = r->buttons;
        ipc->block->shifter_gear = r->shifter_gear;
        /* Set the flag AFTER the values, so a reader that sees has_reading is looking at a
         * fully written set rather than a half-updated one. */
        ipc->block->has_reading = 1u;
    } else {
        ipc->block->has_reading = 0u;
    }
    /* Heartbeat last: the reader takes a fresh stamp as "everything above is current". */
    ipc->block->shim_tick_ms = GetTickCount64();
}
