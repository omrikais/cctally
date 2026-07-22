import { useFullPayload } from '../hooks/useFullPayload';
import { useConversationRef } from './TranscriptContext';
import { SpinnerIcon } from './ConvIcons';

export function NativePayloadDisclosure({
  blockKey,
  which,
  label,
}: {
  blockKey: string;
  which: 'input' | 'result' | 'event';
  label: 'request' | 'output' | 'event';
}) {
  const conversationRef = useConversationRef();
  const state = useFullPayload(conversationRef, blockKey, which);
  const aria = `Load raw ${label} payload`;

  if (state.status === 'done') {
    const payload = state.data;
    const text = payload.which === 'input'
      ? JSON.stringify(payload.input, null, 2)
      : payload.text;
    return (
      <details className="conv-native-raw" open>
        <summary>raw {label} payload</summary>
        <pre>{text}</pre>
      </details>
    );
  }
  if (state.status === 'loading') {
    return <span className="conv-native-raw-loading"><SpinnerIcon /> loading raw {label}…</span>;
  }
  if (state.status === 'error') {
    return <span className="conv-native-raw-error">raw {label} {state.error}</span>;
  }
  return (
    <button
      type="button"
      className="conv-native-raw-btn"
      aria-label={aria}
      disabled={!conversationRef}
      onClick={(event) => { event.stopPropagation(); void state.load(); }}
    >
      raw {label}
    </button>
  );
}
