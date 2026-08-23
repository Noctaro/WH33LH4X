/*
 * The shim's one logging channel, shared by every translation unit.
 *
 * It appends to %TEMP%\wh33lh4x_shim.log. %TEMP% rather than the game's own directory
 * because a game usually lives under Program Files, which a non-elevated process cannot
 * write -- and a proxy that silently fails to log because of that is miserable to diagnose.
 */

#ifndef WH33LH4X_SHIM_LOG_H
#define WH33LH4X_SHIM_LOG_H

void shim_log(const char *fmt, ...);

#endif /* WH33LH4X_SHIM_LOG_H */
