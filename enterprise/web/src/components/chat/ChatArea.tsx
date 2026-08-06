import { DeviceContextCard } from './DeviceContextCard';
import { MessageList } from './MessageList';
import { QuestionInput } from './QuestionInput';
import type { Conversation, ChatMessage } from '../../api/types';

interface ChatAreaProps {
  activeConversation: Conversation | null;
  messages: ChatMessage[];
  isStreaming: boolean;
  inputDisabled?: boolean;
  onSend: (question: string) => void;
  onCancel: () => void;
  onCitationClick: (citationId: string) => void;
}

export function ChatArea({
  activeConversation,
  messages,
  isStreaming,
  inputDisabled,
  onSend,
  onCancel,
  onCitationClick,
}: ChatAreaProps) {
  const disabled = inputDisabled ?? !activeConversation;

  return (
    <div className="flex flex-col h-full">
      <DeviceContextCard conversation={activeConversation} />
      <MessageList messages={messages} onCitationClick={onCitationClick} />
      <QuestionInput
        onSend={onSend}
        onCancel={onCancel}
        isStreaming={isStreaming}
        disabled={disabled}
      />
    </div>
  );
}
