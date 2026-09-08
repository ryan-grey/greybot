/*
 * discord-local-log: a capture-only Vencord plugin.
 *
 * Records the messages the Discord client has ALREADY received and rendered
 * - DMs, group DMs, and every server you can see - to a local file that a
 * separate ingest script folds into a SQLite database for discord-mcp.
 *
 * What it never does: send, edit, delete, or call Discord's REST API on its
 * own. It listens to the client's own Flux events (MESSAGE_CREATE / UPDATE /
 * DELETE / DELETE_BULK / LOAD_MESSAGES_SUCCESS), so scrolling back in a
 * channel backfills history as you browse.
 *
 * The one exception is opt-in and deliberate: the chat-bar "Backfill" button
 * asks the client to load the current conversation's history page by page,
 * paced like a human scrolling, until it reaches the top. That is the same
 * request scrolling makes, but issued by a script - it is the user's call.
 */

import { ChatBarButton, ChatBarButtonFactory } from "@api/ChatButtons";
import * as DataStore from "@api/DataStore";
import { definePluginSettings } from "@api/Settings";
import definePlugin, { OptionType, PluginNative } from "@utils/types";
import { findByPropsLazy } from "@webpack";
import { ChannelStore, GuildStore, MessageActions, MessageStore, Toasts, useEffect, UserStore, useState } from "@webpack/common";

const Native = VencordNative.pluginHelpers.DiscordLocalLog as PluginNative<typeof import("./native")>;
const MessageFetcher = findByPropsLazy("fetchMessages");

const settings = definePluginSettings({
    captureDMs: {
        type: OptionType.BOOLEAN,
        description: "Record direct messages and group DMs",
        default: true,
    },
    captureServers: {
        type: OptionType.BOOLEAN,
        description: "Record server channels",
        default: true,
    },
    ignoredChannels: {
        type: OptionType.STRING,
        description: "Channel ids to never record, comma separated",
        default: "",
    },
    backfillPaceSeconds: {
        type: OptionType.NUMBER,
        description: "Backfill: seconds to wait between pages of 100 (Discord allows about one request a second per conversation; 3-6 looks like a human scroll)",
        default: 1.5,
    },
});

// Direct messages are type 1, group DMs type 3.
const DM_TYPES = new Set([1, 3]);
const FLUSH_EVERY_MS = 2000;
const FLUSH_AT = 200;
const PAGE = 100; // the API maximum per request

// ------------------------------------------------------------ capture --

let queue: string[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;

function flush() {
    if (timer) {
        clearTimeout(timer);
        timer = null;
    }
    if (!queue.length) return;
    const lines = queue.join("\n") + "\n";
    queue = [];
    Native.append(lines).catch(e => console.error("[DiscordLocalLog] append failed", e));
}

function enqueue(event: Record<string, unknown>) {
    queue.push(JSON.stringify({ ...event, at: new Date().toISOString() }));
    if (queue.length >= FLUSH_AT) flush();
    else if (!timer) timer = setTimeout(flush, FLUSH_EVERY_MS);
}

function wanted(channelId: string): boolean {
    const ignored = settings.store.ignoredChannels.split(",").map(s => s.trim()).filter(Boolean);
    if (ignored.includes(channelId)) return false;
    const c = ChannelStore.getChannel(channelId);
    if (!c) return settings.store.captureServers; // unknown: treat as a server channel
    return DM_TYPES.has(c.type) ? settings.store.captureDMs : settings.store.captureServers;
}

function userName(id: string): string {
    const u = UserStore.getUser(id);
    return u ? (u.globalName || u.username) : id;
}

function shapeChannel(channelId: string) {
    const c = ChannelStore.getChannel(channelId);
    if (!c) return { id: channelId };
    const guild = c.guild_id ? GuildStore.getGuild(c.guild_id) : null;
    const recipients: string[] = c.recipients ?? [];
    return {
        id: c.id,
        guild_id: c.guild_id ?? null,
        guild_name: guild?.name ?? null,
        name: c.name || recipients.map(userName).join(", ") || null,
        type: c.type,
        recipients,
    };
}

function isoOf(ts: any): string | null {
    if (!ts) return null;
    if (typeof ts === "string") return ts;
    if (typeof ts.toISOString === "function") return ts.toISOString();
    return null;
}

/** Flux hands us either the raw API payload (snake_case) or a Message
 *  record (camelCase) depending on the event; accept both. */
function shapeMessage(m: any) {
    const a = m.author ?? {};
    return {
        id: m.id,
        channel_id: m.channel_id ?? m.channelId,
        author_id: a.id ?? null,
        author_name: a.global_name ?? a.globalName ?? a.username ?? null,
        content: m.content ?? null,
        ts: isoOf(m.timestamp),
        edited_ts: isoOf(m.edited_timestamp ?? m.editedTimestamp),
        attachments: (m.attachments ?? []).map((x: any) => ({ name: x.filename, url: x.url })),
    };
}

function record(op: "create" | "backfill" | "update", m: any) {
    if (!m?.id) return;
    const channelId = m.channel_id ?? m.channelId;
    if (!channelId || !wanted(channelId)) return;
    enqueue({ kind: "message", op, channel: shapeChannel(channelId), message: shapeMessage(m) });
    if (backfill.running && backfill.channelId === channelId) {
        backfill.captured++;
        notify();
    }
}

// ----------------------------------------------------------- backfill --

interface Backfill {
    running: boolean;
    channelId: string | null;
    pages: number;
    captured: number;
    /** Resolves the in-flight page once a load event lands. */
    pending: (() => void) | null;
}

const backfill: Backfill = { running: false, channelId: null, pages: 0, captured: 0, pending: null };
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(fn => fn());

// ------------------------------------------------------------- estimate --
//
// Discord never says how many messages a conversation holds, so the ETA is
// built from time instead: each page covers some span of calendar time, the
// DM's creation date (from its snowflake) bounds how far back there is to go,
// and the recent pages' span-per-page and seconds-per-page give the rest.
// Rough early, steadier as pages accumulate.

const DISCORD_EPOCH_MS = 1420070400000;
const snowflakeMs = (id: string) => Number(BigInt(id) >> 22n) + DISCORD_EPOCH_MS;

interface PageMark { at: number; oldestMs: number; }
const progress: { createdMs: number; startedAt: number; marks: PageMark[]; } = { createdMs: 0, startedAt: 0, marks: [] };
let ticker: ReturnType<typeof setInterval> | null = null;

function resetProgress(channelId: string) {
    progress.createdMs = snowflakeMs(channelId);
    progress.startedAt = Date.now();
    progress.marks = [];
}

function markPage(oldestId: string | null) {
    if (!oldestId) return;
    progress.marks.push({ at: Date.now(), oldestMs: snowflakeMs(oldestId) });
}

function fmtDuration(sec: number): string {
    if (sec < 60) return `${Math.max(1, Math.round(sec))}s`;
    const m = Math.round(sec / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

/** {label, pct} while running; label is what the chat bar shows. */
function estimate(): { label: string; pct: number | null; } {
    const { marks, createdMs, startedAt } = progress;
    const now = Date.now();
    const elapsed = (now - startedAt) / 1000;
    if (marks.length < 2) return { label: `${fmtDuration(elapsed)} elapsed · estimating…`, pct: null };
    const last = marks[marks.length - 1];
    const window = marks.slice(-10);
    const spanPerPage = (window[0].oldestMs - last.oldestMs) / (window.length - 1);
    const secPerPage = (last.at - window[0].at) / 1000 / (window.length - 1);
    const total = Math.max(1, now - createdMs);
    const remainingSpan = Math.max(0, last.oldestMs - createdMs);
    const pct = Math.max(0, Math.min(99, Math.round((1 - remainingSpan / total) * 100)));
    if (spanPerPage <= 0 || secPerPage <= 0) return { label: `${pct}% · estimating…`, pct };
    const remainingSec = (remainingSpan / spanPerPage) * secPerPage;
    return { label: `${pct}% · ~${fmtDuration(remainingSec)} left`, pct };
}

// Conversations whose history has been backfilled to the top. Persisted in
// Vencord's DataStore and shown as a green check on the DM list item, via a
// stylesheet keyed on Discord's data-list-item-id attribute.
const COMPLETE_KEY = "DiscordLocalLog_complete";
const STYLE_ID = "vc-dll-style";
const complete = new Set<string>();

function renderCompleteStyle() {
    let el = document.getElementById(STYLE_ID) as HTMLStyleElement | null;
    if (!el) {
        el = document.createElement("style");
        el.id = STYLE_ID;
        document.head.appendChild(el);
    }
    const rules = [...complete].map(id =>
        `[data-list-item-id$="___${id}"]{position:relative}` +
        `[data-list-item-id$="___${id}"]::after{content:"\\2713";position:absolute;right:30px;top:50%;` +
        `transform:translateY(-50%);color:#23a55a;font-weight:700;font-size:14px;line-height:1;pointer-events:none}`
    );
    el.textContent =
        ".vc-dll-active{color:#23a55a}\n" +
        ".vc-dll-eta{align-self:center;white-space:nowrap;font-size:12px;color:var(--text-muted);" +
        "margin:0 6px 0 4px;font-variant-numeric:tabular-nums}\n" +
        rules.join("\n");
}

async function loadComplete() {
    try {
        const saved = await DataStore.get<string[]>(COMPLETE_KEY);
        complete.clear();
        for (const id of saved ?? []) complete.add(id);
    } catch (e) {
        console.error("[DiscordLocalLog] could not load completion marks", e);
    }
    renderCompleteStyle();
}

async function markComplete(channelId: string) {
    complete.add(channelId);
    renderCompleteStyle();
    notify();
    try {
        await DataStore.set(COMPLETE_KEY, [...complete]);
        await saveResume(channelId, null);
    } catch (e) {
        console.error("[DiscordLocalLog] could not save completion marks", e);
    }
}

// Where an unfinished backfill got to, per channel, so stopping (or
// switching channels, or restarting Discord) never means starting over.
const RESUME_KEY = "DiscordLocalLog_resume";

async function loadResume(channelId: string): Promise<string | null> {
    try {
        const all = (await DataStore.get<Record<string, string>>(RESUME_KEY)) ?? {};
        return all[channelId] ?? null;
    } catch {
        return null;
    }
}

async function saveResume(channelId: string, oldestId: string | null) {
    try {
        const all = (await DataStore.get<Record<string, string>>(RESUME_KEY)) ?? {};
        if (oldestId) all[channelId] = oldestId;
        else delete all[channelId];
        await DataStore.set(RESUME_KEY, all);
    } catch (e) {
        console.error("[DiscordLocalLog] could not save resume point", e);
    }
}

const olderOf = (a: string | null, b: string | null) =>
    !a ? b : !b ? a : (BigInt(a) < BigInt(b) ? a : b);

function oldestId(messages: any[]): string | null {
    let min: bigint | null = null;
    for (const m of messages) {
        try {
            const v = BigInt(m.id);
            if (min === null || v < min) min = v;
        } catch { /* not a snowflake */ }
    }
    return min === null ? null : min.toString();
}

function sleep(ms: number) {
    return new Promise(r => setTimeout(r, ms));
}

function toast(message: string, type = Toasts.Type.MESSAGE) {
    Toasts.show({ message, type, id: Toasts.genId(), options: { duration: 4000, position: Toasts.Position.BOTTOM } });
}

/** The client's own view of a channel: oldest loaded id and whether it
 *  believes more history exists above it. */
function held(channelId: string): { oldest: string | null; more: boolean; } {
    const h: any = MessageStore.getMessages(channelId);
    if (!h) return { oldest: null, more: true };
    const arr: any[] = typeof h.toArray === "function" ? h.toArray() : (h._array ?? []);
    const oldest = h.first?.()?.id ?? oldestId(arr);
    return { oldest, more: h.hasMoreBefore !== false };
}

function fetcher() {
    const m: any = MessageActions;
    if (m && typeof m.fetchMessages === "function") return m;
    return MessageFetcher;
}

async function loadPage(channelId: string, before: string | null) {
    // Resolve on the load event (fast path) or when the fetch call itself
    // settles (cached / short-circuited path), whichever comes first.
    const viaEvent = new Promise<void>(resolve => { backfill.pending = () => resolve(); });
    const viaCall = Promise.resolve(fetcher().fetchMessages({
        channelId, limit: PAGE, before: before ?? undefined,
    })).then(() => sleep(250));
    const timeout = sleep(20_000).then(() => { throw new Error("no response in 20s"); });
    try {
        await Promise.race([viaEvent, viaCall, timeout]);
    } finally {
        backfill.pending = null;
    }
}

async function runBackfill(channelId: string) {
    if (backfill.running) return;
    Object.assign(backfill, { running: true, channelId, pages: 0, captured: 0, pending: null });
    resetProgress(channelId);
    ticker = setInterval(notify, 1000); // keeps the ETA ticking between pages
    notify();

    let reason = "reached the top";
    try {
        // Pick up from whichever is older: what the client still holds, or
        // the point a previous run reached (survives stop / switch / restart).
        let before = olderOf(held(channelId).oldest, await loadResume(channelId));
        if (!before) {
            // Nothing loaded yet: bring in the latest page first.
            await loadPage(channelId, null);
            before = held(channelId).oldest;
            backfill.pages++;
            notify();
            if (!before) throw new Error("channel has no messages loaded");
        }
        markPage(before);
        while (backfill.running) {
            await loadPage(channelId, before);
            backfill.pages++;
            const now = held(channelId);
            // The store may also hold newer pages; only accept a genuinely older id.
            const reached = olderOf(before, now.oldest);
            markPage(reached);
            notify();
            if (reached === before) {
                if (!now.more) break;
                throw new Error("page loaded nothing older");
            }
            before = reached;
            await saveResume(channelId, before);
            if (!now.more) break;
            const pace = Math.max(1, settings.store.backfillPaceSeconds) * 1000;
            // Wait in the main process so a minimized window does not slow the
            // pace; fall back to a renderer timer if the IPC call fails.
            await Native.sleep(pace + Math.random() * pace * 0.5).catch(() => sleep(pace));
        }
        if (!backfill.running) reason = "stopped";
        else await markComplete(channelId);
    } catch (e: any) {
        reason = `stalled (${e?.message ?? e})`;
    } finally {
        if (ticker) {
            clearInterval(ticker);
            ticker = null;
        }
        flush();
        backfill.running = false;
        backfill.pending = null;
        notify();
        toast(`DiscordLocalLog: backfill ${reason} - ${backfill.captured} messages over ${backfill.pages} pages`,
            reason.startsWith("stalled") ? Toasts.Type.FAILURE : Toasts.Type.SUCCESS);
    }
}

// "Backfill every DM": conversations wait here and run one after another,
// each at the configured pace, skipping ones already marked complete.
const dmQueue: string[] = [];
let queueTotal = 0;

function stopBackfill() {
    dmQueue.length = 0; // stopping also drains the queue
    backfill.running = false;
    notify();
}

async function runQueue() {
    while (dmQueue.length) {
        const id = dmQueue.shift()!;
        await runBackfill(id);
    }
    if (queueTotal) toast(`DiscordLocalLog: DM queue finished (${queueTotal} conversations)`, Toasts.Type.SUCCESS);
    queueTotal = 0;
}

function backfillAllDMs() {
    if (backfill.running) return;
    const ids = ChannelStore.getSortedPrivateChannels()
        .filter(c => DM_TYPES.has(c.type) && !complete.has(c.id) && wanted(c.id))
        .map(c => c.id);
    if (!ids.length) {
        toast("DiscordLocalLog: every DM is already fully captured", Toasts.Type.SUCCESS);
        return;
    }
    dmQueue.push(...ids);
    queueTotal = ids.length;
    toast(`DiscordLocalLog: backfilling ${ids.length} DMs one after another - click the button to stop`);
    runQueue();
}

const BackfillIcon = ({ height = 20, width = 20, className }: { height?: number; width?: number; className?: string; }) => (
    <svg viewBox="0 0 24 24" width={width} height={height} className={className} fill="currentColor">
        <path d="M4 4h16v4H4V4zm1 6h14v10H5V10zm4 2v2h6v-2H9z" />
    </svg>
);

const BackfillButton: ChatBarButtonFactory = ({ channel, isMainChat }) => {
    const [, tick] = useState(0);
    useEffect(() => {
        const fn = () => tick(n => n + 1);
        listeners.add(fn);
        return () => void listeners.delete(fn);
    }, []);
    if (!isMainChat || !channel) return null;

    const mine = backfill.running && backfill.channelId === channel.id;
    const busyElsewhere = backfill.running && !mine;
    const done = complete.has(channel.id);
    const tooltip = mine
        ? `Backfilling: ${backfill.pages} pages, ${backfill.captured} messages - click to stop`
        : busyElsewhere
            ? "DiscordLocalLog is backfilling another conversation"
            : done
                ? "History fully captured (click to backfill again · right-click: every DM)"
                : "Backfill this conversation's history (right-click: every DM in the list)";
    const queued = dmQueue.length ? ` · ${dmQueue.length} more DMs queued` : "";

    return (
        <>
            {mine && <span className="vc-dll-eta">{estimate().label}{queued}</span>}
            <ChatBarButton
                tooltip={tooltip}
                onClick={() => {
                    if (mine || busyElsewhere) stopBackfill();
                    else {
                        toast("DiscordLocalLog: backfilling - progress is in the chat bar");
                        runBackfill(channel.id);
                    }
                }}
                onContextMenu={e => {
                    e.preventDefault();
                    backfillAllDMs();
                }}
            >
                <BackfillIcon className={mine ? "vc-dll-active" : undefined} />
            </ChatBarButton>
        </>
    );
};

// ------------------------------------------------------------- plugin --

export default definePlugin({
    name: "DiscordLocalLog",
    description: "Capture-only log of the messages this client receives, for a local search index. Never sends, edits or deletes. Optional paced backfill button.",
    authors: [{ name: "Ryan Grey", id: 0n }],
    settings,

    chatBarButton: {
        icon: BackfillIcon,
        render: BackfillButton,
    },

    flux: {
        MESSAGE_CREATE({ message, optimistic }: { message: any; optimistic: boolean; }) {
            if (optimistic) return; // the server-acknowledged copy follows
            record("create", message);
        },
        MESSAGE_UPDATE({ message }: { message: any; }) {
            record("update", message);
        },
        MESSAGE_DELETE({ id, channelId }: { id: string; channelId: string; }) {
            if (!wanted(channelId)) return;
            enqueue({ kind: "delete", id, channel_id: channelId });
        },
        MESSAGE_DELETE_BULK({ ids, channelId }: { ids: string[]; channelId: string; }) {
            if (!wanted(channelId)) return;
            enqueue({ kind: "delete", ids, channel_id: channelId });
        },
        LOAD_MESSAGES_SUCCESS({ channelId, messages }: { channelId: string; messages: any[]; }) {
            if (wanted(channelId)) {
                for (const m of messages ?? []) record("backfill", { ...m, channel_id: m.channel_id ?? channelId });
            }
            if (backfill.pending && backfill.channelId === channelId) backfill.pending();
        },
        LOAD_MESSAGES_SUCCESS_CACHED({ channelId }: { channelId: string; }) {
            // Served from the client's cache: nothing new to record, but the
            // backfill loop is waiting and should move on to the next page.
            if (backfill.pending && backfill.channelId === channelId) backfill.pending();
        },
    },

    start() {
        loadComplete();
    },

    stop() {
        stopBackfill();
        flush();
        document.getElementById(STYLE_ID)?.remove();
    },
});

