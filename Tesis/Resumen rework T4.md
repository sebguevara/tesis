# Resumen del rework T4

**Base:** `tesis_v02-06.docx`, que no se modificó. Tiene el mismo texto que la T3 limpia; solo cambian la portada, la Tabla 6 y algunos párrafos vacíos.

**Archivos generados:**
- **Tesis - T4 rework (limpia).docx**: versión final.
- **Tesis - T4 rework (cambios marcados).docx**: todo lo nuevo o modificado aparece resaltado en cyan, igual que en T3.

Se conservaron el formato, las portadas de capítulo, las imágenes y los estilos. El documento pasó de 59 a 74 páginas.

## El enfoque

Toda la tesis se reorganizó alrededor de esta pregunta:

> **¿Es posible transformar automáticamente un sitio web institucional en un asistente conversacional en el que la institución pueda confiar?**

La tesis central es que la confiabilidad dependió más de dos cosas que de hacer el sistema más "inteligente":
- encontrar la evidencia correcta;
- saber decir "no sé".

## Supuestos que tomé (confirmalos)

1. **Caso de estudio:** solo med.unne.edu.ar. Aplicarlo a otras instituciones quedó como trabajo futuro.
2. **Usuario principal:** el estudiante o aspirante que consulta. La institución que instala el widget queda en segundo plano.
3. **Configuración descrita:** la **Etapa 5**, que es la que tiene métricas medidas. Allí el umbral del verificador es 0,6 y se usan los 12 fragmentos mejor puntuados.
   - El código actual ya cambió (Etapas 6/7 sin commitear). Hoy usa umbral 0,4, top 18 y expansión a fragmentos vecinos.
   - Esas etapas no tienen corridas de evaluación en `eval/results/`.
   - Si las medís, hay que actualizar la sección 4.9 (Tabla 12 y Fig. 22) y los valores en 4.4.8 y 4.5.3.

## Cambios por capítulo

**Resumen.** Reescrito completo: pregunta, enfoque, resultados medidos, hallazgo principal y limitaciones.

**Capítulo I**
- 1.1: la brecha ahora incluye que RAG reduce la alucinación pero no la elimina, y que en información institucional el error tiene costo.
- Nuevo apartado **"Pregunta de investigación"**, con la definición operativa de "confiable" y la hipótesis.
- Objetivo general: agrega "responder de forma confiable" y "cuantificar su confiabilidad".
- Objetivo específico 4: agrega la verificación del respaldo.
- Nuevo **objetivo específico 6**: evaluación cuantitativa de la confiabilidad.

**Capítulo II (solo correcciones de hechos)**
- Fragmentos de 500 caracteres con 50 de solapamiento, más contexto por fragmento (antes decía 1500/300).
- Modelo de embeddings: `text-embedding-3-large` a 1536 dimensiones (antes decía "small").
- Extracción: Crawl4AI sobre Playwright.
- Recuperación: búsqueda híbrida + RRF + reranker.
- Fase 4 (Aseguramiento de calidad):
  - "Revisión humana de grounding" pasó a "evaluación automática con LLM como juez e inspección manual de errores". La validación humana sistemática no existió.
  - Ahora remite a la sección 4.9.

**Capítulo III (solo correcciones de hechos)**
- LangGraph: describe el grafo de 4 nodos.
- Se agregó Crawl4AI.
- Nuevo apartado sobre búsqueda de texto completo y cross-encoder.

**Capítulo IV**
- **4.2:** se conserva. Se reescribió 4.2.9: fragmentos de 500/50, contextual retrieval, por qué se achicaron, y costo de USD 3,6 y unos 19 minutos por rastreo.
- **4.3:** se conserva la teoría.
  - Se quitó el "grounding 0,80".
  - La Tabla 5 ahora refleja la tabla real `chunks`, con las columnas `context` y `token_count`.
- **4.4:** nuevas secciones:
  - 4.4.6: límites de la búsqueda vectorial.
  - 4.4.7: búsqueda híbrida y RRF.
  - 4.4.8: reranker.
  - Las secciones siguientes se renumeraron (4.4.9 a 4.4.11). La pregunta puente ahora es *"¿cómo se controla que el sistema responda solo lo que la evidencia respalda?"*.
- **4.5:** reescrita completa.
  - Cuenta el agente iterativo como **arquitectura inicial** y explica por qué se abandonó: más de 4.000 líneas, 18 % de alucinación y ~14 consultas de 50 terminaban por tiempo de espera.
  - Describe el flujo final de 4 pasos.
  - Nueva sección 4.5.4, *Saber decir "no sé": el verificador*.
  - Nueva sección 4.5.5, temperatura 0 y control de latencia.
  - Se eliminaron las viejas 4.5.6 y 4.5.7.
- **4.6:** la memoria ahora se describe como el paso de reescritura (últimos 6 turnos, hasta 1500 caracteres). La Tabla 9 refleja la tabla real de mensajes. La síntesis arquitectónica se reescribió en 4 capas.
- **4.9 (nueva): Evaluación experimental y evolución del sistema.**
  - Conjunto de 50 preguntas en 10 categorías (Tabla 11).
  - Métricas y procedimiento.
  - Evolución por etapas (Tabla 12 y Fig. 22).
  - Resultados por categoría (Tabla 13).
  - Casos Q049, Q050, Q003 y Q024.
  - Costos, latencia e intentos descartados (timeout de 20 s y sitemap-first).

**Figuras y tablas**
- **Fig. 9:** la imagen vieja del agente ahora está rotulada como "arquitectura inicial" y se movió a 4.5.1.
- **Fig. 10:** **nuevo diagrama** del flujo final de 4 pasos. Reemplaza al diagrama de secuencia viejo, que se eliminó.
- **Fig. 22:** nuevo gráfico de evolución de las métricas.
- **Tablas 7 y 8:** reescritas (pasos del flujo y reglas del verificador).
- **Tablas 11 a 13:** nuevas.
- Los números de página de los índices de figuras y tablas se recalcularon con un render de Word, así que deberían ser exactos.

**Capítulo V**
- 5.1: responde la pregunta, presenta las **5 conclusiones** y retoma los objetivos OE1 a OE6.
- 5.2: limitaciones honestas:
  - n = 50, sin significancia estadística;
  - dataset armado por el autor;
  - juez sin validar contra calificación humana;
  - un solo sitio;
  - listados incompletos, evidencia engañosa y latencia.
- 5.3: trabajo futuro nuevo:
  - validar el juez contra calificación humana;
  - probar en otros sitios;
  - rerankers más fuertes y mejor manejo de listados;
  - menor latencia;
  - corpus que cambia con el tiempo.
  - Se sacaron de trabajo futuro la búsqueda híbrida, el reranking y la evaluación automática, porque ya están implementados.
- 5.4: el aporte ya no es "comprender RAG". Ahora es doble: el prototipo y la evaluación de qué decisiones lo hacen confiable.

**Referencias.** Se agregaron de [21] a [27], todas citadas en el texto:
- [21] RRF (Cormack et al., 2009)
- [22] Nogueira y Cho
- [23] MS MARCO
- [24] Contextual Retrieval de Anthropic
- [25] LLM-as-a-Judge (Zheng et al.)
- [26] RAGAS
- [27] Crawl4AI

## Transparencia en los números

Todas las cifras salen de `eval/results/scored_*.json` y de `docs/RAG_ITERATIONS.md`.

- **Cambio de juez:** se declara explícitamente. Las etapas 0 a 4 se calificaron con gpt-4o-mini y la etapa 5 con gpt-4o. Para mostrar que la mejora no viene solo del juez, se cita que la corrida E4 dio 8 % de alucinación con ambos jueces.
- **Alucinación:** se reporta como **0–2 %**, porque la corrida previa de la etapa 5 (`05b`) dio 2 %.
- **Regla de no regresión:** se violó en E3 y E4. Las dos excepciones están documentadas y justificadas.
- **Fidelidad de E5:** se usó 0,97, el valor del JSON. La bitácora dice 1,000 en un lugar, pero manda el JSON.

## Pendientes que requieren acción tuya

1. **Rehacer las Fig. 12 y 13.** Todavía muestran el ciclo viejo con reintentos y el umbral 0,80. En el documento hay una nota `[FIGURA A REHACER: …]` debajo de cada una, visible también en la versión limpia. Borrala cuando las reemplaces.
2. **Tapar las claves en las Fig. 14 y 19.** Muestran claves `pfc_sk_live_…` completas: tapalas en las imágenes o rotá las claves antes de entregar.
3. **Actualizar el Índice general** (clic derecho → Actualizar campo). No incluye 4.8 ni la nueva 4.9.
4. **Confirmar los dos supuestos** de arriba (caso de estudio y usuario principal).
5. **Si volvés a correr la evaluación con el código actual** (Etapas 6/7), actualizá la Tabla 12, la Fig. 22 y los parámetros (umbral 0,4, top 18).
6. **Si cambiás el título del Cap. IV.** Hoy se sigue llamando "Resultados", porque la portada tiene ese texto dentro de la imagen.
7. **Revisá la redacción** de los párrafos en cyan para confirmar que suene a vos.
8. **Página casi vacía antes de la portada del Cap. V.** Con la paginación de Word queda una página con una sola línea ("segundos."). Revisalo en la versión final.

## Arreglo adicional

En la v02-06, la portada del Cap. V estaba anclada al párrafo 4.8.5 y, en Word, tapaba la página de la Fig. 21. Se movió después de la sección 4.9, en una página propia.
