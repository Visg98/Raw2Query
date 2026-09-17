import { apiFetch } from "./client";

/**
 * Chat sessions for the Ask page.
 *
 * There is deliberately no `createChat`: a session row is created by
 * `POST /query` on the first question asked in it (see api/query.js), so
 * opening "New chat" in the UI persists nothing until the user actually
 * asks something.
 */
export function listChats() {
  return apiFetch("/chats");
}

export function getChat(chatId) {
  return apiFetch(`/chats/${chatId}`);
}

export function renameChat(chatId, title) {
  return apiFetch(`/chats/${chatId}`, { method: "PATCH", json: { title } });
}

export function deleteChat(chatId) {
  return apiFetch(`/chats/${chatId}`, { method: "DELETE" });
}
