<script lang="ts">
  import ConversationalWidget from "./lib/ConversationalWidget.svelte";

  let { onClose = () => {} }: { onClose?: () => void } = $props();
</script>

<ConversationalWidget {onClose} />
