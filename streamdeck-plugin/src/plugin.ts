import streamDeck from "@elgato/streamdeck";

import { actions, refreshAll } from "./actions/merkzettel";

for (const a of actions) {
	streamDeck.actions.registerAction(a);
}

// Jede Sekunde den Zustand vom Stream-Merkzettel holen und die Tasten beschriften
setInterval(refreshAll, 1000);

streamDeck.connect();
