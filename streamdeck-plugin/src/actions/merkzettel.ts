import streamDeck, { action, KeyDownEvent, SingletonAction } from "@elgato/streamdeck";
import * as http from "node:http";

// Adresse des Stream-Merkzettels (muss zu SD_PORT im Programm passen)
const HOST = "127.0.0.1";
const PORT = 8765;
const log = streamDeck.logger;

/** Kleine HTTP-Anfrage über Nodes eigenes http-Modul (robuster als fetch). */
function request(method: "GET" | "POST", path: string, timeoutMs: number): Promise<{ status: number; body: string }> {
	return new Promise((resolve, reject) => {
		const req = http.request(
			{ host: HOST, port: PORT, path, method, headers: { "Content-Length": "0" }, timeout: timeoutMs },
			(res) => {
				let body = "";
				res.setEncoding("utf8");
				res.on("data", (c: string) => (body += c));
				res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
			},
		);
		req.on("timeout", () => req.destroy(new Error(`Zeitüberschreitung nach ${timeoutMs} ms`)));
		req.on("error", reject);
		req.end();
	});
}

function describe(e: unknown): string {
	const err = e as { message?: string; code?: string; cause?: { code?: string; message?: string } };
	return [err.code, err.message, err.cause?.code, err.cause?.message].filter(Boolean).join(" | ") || String(e);
}

type Status = {
	stream: string | null;
	time: string | null;
	running: boolean;
	away: boolean;
	awaySince?: string | null;
	clips: number;
	clipsDone?: number;
	live?: boolean;
};

/** Befehl an den Stream-Merkzettel schicken (clip, gap, clock). */
async function send(cmd: string): Promise<boolean> {
	try {
		const res = await request("POST", `/${cmd}`, 2000);
		const ok = res.status >= 200 && res.status < 300;
		if (!ok) log.warn(`POST /${cmd} -> HTTP ${res.status}: ${res.body}`);
		return ok;
	} catch (e) {
		log.warn(`POST /${cmd} fehlgeschlagen: ${describe(e)}`);
		return false;
	}
}

let lastError = "";
/** Aktuellen Zustand abfragen, null = Programm nicht erreichbar. */
async function getStatus(): Promise<Status | null> {
	try {
		const res = await request("GET", "/status", 1500);
		if (res.status !== 200) throw new Error(`HTTP ${res.status}`);
		if (lastError) log.info("Verbindung zum Stream-Merkzettel steht wieder");
		lastError = "";
		return JSON.parse(res.body) as Status;
	} catch (e) {
		const msg = describe(e);
		if (msg !== lastError) log.warn(`GET /status fehlgeschlagen: ${msg}`); // nur einmal loggen
		lastError = msg;
		return null;
	}
}

type KeyLike = {
	id: string;
	setTitle?: (t: string) => Promise<void>;
	setState?: (s: number) => Promise<void>;
};

/** Gemeinsame Logik aller Tasten. */
abstract class MerkzettelAction extends SingletonAction {
	protected abstract readonly cmd: string;
	protected abstract title(st: Status): string;
	/** Hat die Taste zwei Zustände (Bild wechselt)? */
	protected readonly multiState: boolean = false;
	protected state(_st: Status): number {
		return 0;
	}

	private last = new Map<string, string>();

	override async onKeyDown(ev: KeyDownEvent): Promise<void> {
		const ok = await send(this.cmd);
		try {
			if (ok) await ev.action.showOk();
			else await ev.action.showAlert();
		} catch (e) {
			log.error(`Feedback fehlgeschlagen: ${String(e)}`);
		}
		void refreshAll();
	}

	async refresh(st: Status | null): Promise<void> {
		let text: string;
		let state = 0;
		if (!st) text = "Programm\naus";
		else if (!st.stream) text = "Kein\nStream";
		else {
			text = this.title(st);
			state = this.state(st);
		}

		for (const a of this.actions) {
			const k = a as unknown as KeyLike;
			const key = `${text}|${state}`;
			if (this.last.get(k.id) === key) continue;
			this.last.set(k.id, key);
			// Erst Text, dann (nur bei Tasten mit zwei Bildern) den Zustand – jeweils einzeln abgesichert
			try {
				if (typeof k.setTitle === "function") await k.setTitle(text);
			} catch (e) {
				log.error(`setTitle fehlgeschlagen: ${String(e)}`);
			}
			if (this.multiState) {
				try {
					if (typeof k.setState === "function") await k.setState(state);
				} catch (e) {
					log.error(`setState fehlgeschlagen: ${String(e)}`);
				}
			}
		}
	}
}

@action({ UUID: "com.xjanx.clipp-helper.clip" })
export class ClipAction extends MerkzettelAction {
	protected readonly cmd = "clip";
	protected title(st: Status): string {
		if (!st.clips) return "Clip";
		return `${st.clipsDone ?? 0}/${st.clips}`;
	}
}

@action({ UUID: "com.xjanx.clipp-helper.gap" })
export class GapAction extends MerkzettelAction {
	protected readonly cmd = "gap";
	protected override readonly multiState = true;
	protected title(st: Status): string {
		return st.away ? `weg seit\n${st.awaySince ?? ""}` : "Bin weg";
	}
	protected override state(st: Status): number {
		return st.away ? 1 : 0;
	}
}

@action({ UUID: "com.xjanx.clipp-helper.clock" })
export class ClockAction extends MerkzettelAction {
	protected readonly cmd = "clock";
	protected override readonly multiState = true;
	protected title(st: Status): string {
		return (st.live ? "LIVE\n" : "") + (st.time ?? "-");
	}
	protected override state(st: Status): number {
		return st.running ? 0 : 1;
	}
}

export const actions = [new ClipAction(), new GapAction(), new ClockAction()];

let busy = false;
/** Alle sichtbaren Tasten mit dem aktuellen Zustand beschriften. */
export async function refreshAll(): Promise<void> {
	if (busy) return;
	busy = true;
	try {
		const st = await getStatus();
		for (const a of actions) {
			try {
				await a.refresh(st);
			} catch (e) {
				log.error(`refresh fehlgeschlagen: ${String(e)}`);
			}
		}
	} finally {
		busy = false;
	}
}
