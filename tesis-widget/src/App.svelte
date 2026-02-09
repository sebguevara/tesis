<script lang="ts">
  import ConversationalWidget from "./lib/ConversationalWidget.svelte";

  let {
    onClose = () => {},
    onRefresh = () => {},
    onExpand = () => {},
    endpoint = "/api/widget/query",
    apiKey = "",
    sourceId = "",
    sessionId = "",
    metadata = {},
  }: {
    onClose?: () => void;
    onRefresh?: () => void;
    onExpand?: () => void;
    endpoint?: string;
    apiKey?: string;
    sourceId?: string;
    sessionId?: string;
    metadata?: Record<string, unknown>;
  } = $props();
</script>

<ConversationalWidget
  {onClose}
  {onRefresh}
  {onExpand}
  {endpoint}
  {apiKey}
  {sourceId}
  {sessionId}
  {metadata}
/>
