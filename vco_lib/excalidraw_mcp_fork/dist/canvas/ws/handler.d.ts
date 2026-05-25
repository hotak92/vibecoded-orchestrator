import { WebSocketServer, WebSocket } from 'ws';
import type { Server as HttpServer } from 'node:http';
import type { Config } from '../../shared/config.js';
import type { ServerMessage } from './protocol.js';
import type { ElementStore } from '../store/store.js';
export interface WsContext {
    wss: WebSocketServer;
    clients: Set<WebSocket>;
    broadcast: (message: ServerMessage, exclude?: WebSocket) => void;
}
export declare function createWebSocketServer(httpServer: HttpServer, config: Config, store: ElementStore): WsContext;
//# sourceMappingURL=handler.d.ts.map