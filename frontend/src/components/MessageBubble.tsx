import type {ChatMessage} from "../types/runtime";


function messageText(message: ChatMessage): string {
  if (typeof message.content === "string") {
    return message.content;
  }
  return message.content
    .filter((block) => block.type === "text" && block.text)
    .map((block) => block.text)
    .join("\n");
}

export function MessageBubble({message}: {message: ChatMessage}) {
  const text = messageText(message);
  if (!text || text.startsWith("<preflight_context>")) {
    return null;
  }

  const isUser = message.role === "user";
  return (
    <article className={`message message--${message.role}`}>
      {!isUser && <div className="message-avatar">AI</div>}
      <div className="message-body">
        <p className="message-author">{isUser ? "You" : "Agent"}</p>
        <div className="message-copy">{text}</div>
      </div>
    </article>
  );
}
