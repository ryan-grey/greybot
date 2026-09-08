/*
 * Main-process half of discord-local-log. The renderer cannot touch the
 * filesystem, so it hands batches of JSON lines here and this appends them
 * to ~/Library/Application Support/discord-local-log/events.jsonl.
 *
 * That file is the whole interface: ingest.py folds it into discord.db.
 * Nothing here talks to the network.
 */

import { IpcMainInvokeEvent } from "electron";
import { appendFile, mkdir } from "fs/promises";
import { homedir } from "os";
import { join } from "path";

const DIR = join(homedir(), "Library", "Application Support", "discord-local-log");
const FILE = join(DIR, "events.jsonl");

let ready: Promise<void> | null = null;

export async function append(_: IpcMainInvokeEvent, lines: string): Promise<void> {
    ready ??= mkdir(DIR, { recursive: true }).then(() => undefined);
    await ready;
    await appendFile(FILE, lines, "utf8");
}

export function getPath(_: IpcMainInvokeEvent): string {
    return FILE;
}

/** A timer that keeps its pace when the window is minimized: Chromium
 *  throttles renderer timers in hidden windows, the main process does not.
 *  The backfill loop waits here between pages. */
export function sleep(_: IpcMainInvokeEvent, ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, Math.max(0, Math.min(ms, 120_000))));
}

