import './widget';

// Inicializar el widget automáticamente
function initWidget() {
  // Verificar si ya existe
  if (document.querySelector('conversational-widget')) {
    return;
  }

  // Crear y añadir el widget al body
  const widget = document.createElement('conversational-widget');
  document.body.appendChild(widget);

  return widget;
}

// Auto-inicialización cuando el DOM esté listo
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initWidget);
} else {
  initWidget();
}

// Exportar API global
if (typeof window !== 'undefined') {
  (window as any).ConversationalWidget = {
    init: initWidget,
    open: () => {
      const widget = document.querySelector('conversational-widget') as any;
      widget?.open?.();
    },
    close: () => {
      const widget = document.querySelector('conversational-widget') as any;
      widget?.close?.();
    }
  };
}

export { initWidget };
