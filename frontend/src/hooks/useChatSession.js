import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getChat } from "../api/chats";

/**
 * The conversation currently open on the Ask page.
 *
 * Replaces `useChatHistory`, which kept one flat message list in
 * `localStorage` — so there was exactly one conversation per browser, gone
 * on a cleared profile and invisible from any other device. Conversations
 * are server-side records now (`chat_sessions` / `chat_messages`).
 *
 * `activeChatId === null` means an unsaved draft: a chat the user has
 * opened but not yet asked anything in. Nothing is written until the first
 * question, at which point `POST /query` creates the session and returns
 * its id for `adoptChatId`.
 *
 * Turns live in local state rather than being re-read after every answer:
 * the local array *is* the live conversation, so a freshly answered turn
 * can't flicker out while a refetch lands. The server is read when a
 * *different* conversation is opened.
 *
 * `pendingChatId` is what makes that read correct. Seeding on "any payload
 * for this chat id" looked right but replayed whatever was in the query
 * cache, and that snapshot goes stale the moment another turn is added —
 * so reopening a two-question conversation from the sidebar showed only the
 * first question. Seeding waits for a *settled* fetch of the chat that was
 * actually asked for.
 */
export function useChatSession() {
  const [activeChatId, setActiveChatId] = useState(null);
  const [messages, setMessages] = useState([]);
  // The chat whose messages we're waiting to load. Non-null only between
  // selecting a conversation and its fetch settling.
  const [pendingChatId, setPendingChatId] = useState(null);

  const { data: chat, isFetching } = useQuery({
    queryKey: ["chat", activeChatId],
    queryFn: () => getChat(activeChatId),
    enabled: Boolean(activeChatId),
    // Deliberately no `staleTime`: reopening a conversation has to show
    // every turn, including ones appended since it was last read.
  });

  useEffect(() => {
    if (pendingChatId === null) return;
    // `!isFetching` is the important half - react-query hands back cached
    // data immediately and refetches behind it, and the cached copy is
    // exactly the stale snapshot that used to truncate the transcript.
    if (chat && chat.id === pendingChatId && !isFetching) {
      setMessages(chat.messages || []);
      setPendingChatId(null);
    }
  }, [chat, isFetching, pendingChatId]);

  const startNewChat = useCallback(() => {
    setPendingChatId(null);
    setActiveChatId(null);
    setMessages([]);
  }, []);

  const selectChat = useCallback((chatId) => {
    setPendingChatId(chatId);
    setActiveChatId(chatId);
    setMessages([]);
  }, []);

  const adoptChatId = useCallback((chatId) => {
    // The draft just became a real session. The local messages already are
    // that conversation, so don't wait on a read - re-seeding from the
    // server would at best be a no-op and at worst drop an in-flight turn.
    setPendingChatId(null);
    setActiveChatId(chatId);
  }, []);

  const appendMessage = useCallback((message) => {
    setMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), created_at: new Date().toISOString(), ...message },
    ]);
  }, []);

  return {
    activeChatId,
    isDraft: activeChatId === null,
    messages,
    isLoadingMessages: pendingChatId !== null,
    startNewChat,
    selectChat,
    adoptChatId,
    appendMessage,
  };
}
