import { DeviceContextCard } from './DeviceContextCard';
import { MessageList } from './MessageList';
import { QuestionInput } from './QuestionInput';
import type { Conversation, ChatMessage } from '../../api/types';

interface ChatAreaProps {
  activeConversation: Conversation | null;
  messages: ChatMessage[];
  isStreaming: boolean;
  onSend: (question: string) => void;
  onCancel: () => void;
  onCitationClick: (citationId: string) => void;
}

export function ChatArea({
  activeConversation,
  messages,
  isStreaming,
  onSend,
  onCancel,
  onCitationClick,
}: ChatAreaProps) {
  return (
    <div className="flex flex-col h-full">
      <DeviceContextCard conversation={activeConversation} />
      <MessageList messages={messages} onCitationClick={onCitationClick} />
      <QuestionInput
        onSend={onSend}
        onCancel={onCancel}
        isStreaming={isStreaming}
        disabled={!activeConversation}
      />
    </div>
  );
}
