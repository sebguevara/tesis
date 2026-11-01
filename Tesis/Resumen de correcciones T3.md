# Resumen de correcciones — Tesis (T3)

Se generaron **dos versiones** del documento, con todo el formato, imágenes y fuentes originales intactos:

- **Tesis - T3 corregida (limpia).docx** → versión final, sin marcas.
- **Tesis - T3 corregida (cambios marcados).docx** → lo nuevo o modificado está **resaltado en cyan** para que revises cada cambio.

Se quitaron los 14 comentarios de la profesora (ya resueltos) y los resaltados amarillos que tenías como recordatorio.

## Comentarios resueltos

| # | Comentario | Qué se hizo |
|---|------------|-------------|
| 2, 92 | Dos figuras "Fig. 6"; el índice solo listaba una | Renumeradas: la del coseno queda **Fig. 6** y la de HNSW pasa a **Fig. 7**; las antiguas 7→8 … 20→21. Se reconstruyó el **Índice de figuras** (ahora lista las 21, incluida la del coseno que faltaba) y se corrigieron las referencias en el texto. |
| 3, 103 | Tabla 8 y 9 con el mismo título; la 9 no correspondía | La **Tabla 9** pasó a llamarse **"Estructura del modelo de estado de sesión"** (en el cuerpo y en el índice de tablas). |
| 50 | No posiciona el trabajo ni justifica la brecha | Párrafo nuevo al final de **1.1** que ubica el trabajo frente a lo existente y define el aporte concreto. |
| 52 | El objetivo general sonaba teórico ("investigar") | Reescrito en clave ingenieril: **"Diseñar y validar… un sistema conversacional basado en RAG…"**. |
| 54 | Falta el "porqué" en términos de competencias | Párrafo nuevo en **1.3** sobre las competencias del plan (CE1, CE7, CGS3). |
| 57, 59 | Dice CRISP-DM pero describe CRISP-ML(Q) | Títulos **2.1** y **2.2** corregidos a **CRISP-ML(Q)** + párrafo que aclara la diferencia entre ambos marcos. |
| 60 | Los chunks 1500/300 sin justificación técnica | Párrafo nuevo en **4.2.9** que explica el porqué (contexto vs. ruido, ventana del modelo, costo por tokens). |
| 63 | Svelte no aparecía en el Cap. III | Agregado en **3.2** como framework del widget. |
| 116 | "Figura 18" mal + faltaba texto en el widget expandido | Corregido a **"Figura 21"** y se amplió la sección **4.8.5**. |
| 129 | Las conclusiones no retomaban los objetivos específicos | Párrafo nuevo en **5.1** que retoma uno por uno **OE1–OE5**. |
| 134 | Solo 6 referencias; [4] mal capitalizada | [4] corregida a **"Language Models are Few-Shot Learners"**; se agregaron **14 referencias reales** [7]–[20] (HNSW, pgvector, Playwright, Svelte, LangChain/LangGraph, FastAPI, PostgreSQL, embeddings, Docker, surveys de RAG y de alucinaciones) y se **citaron en el texto** donde corresponde. |

## Otros arreglos
- Typos: "para para", "cntrolado", doble punto, "Herbe rt-Voss" → "Herbert-Voss", asteriscos sueltos en las citas NeurIPS.
- Citas [7]–[20] insertadas en las herramientas del Capítulo III y en los puntos técnicos relevantes del Capítulo IV.

## Para revisar antes de la entrega final
1. **Números de página de los índices**: son aproximados (el documento creció ~2 páginas). Los índices de figuras y tablas quedaron como texto fijo, así que podés ajustar los números directamente. El Índice general sigue siendo un campo de Word (clic derecho → Actualizar campo).
2. **Códigos de competencia (CE1 / CE7 / CGS3)**: confirmá que coincidan con los de tu plan de estudios.
3. Revisá los párrafos en cyan de la versión marcada para validar que la redacción suene a vos.

---

# Segunda revisión (relectura, foco, metodología y citas)

## Metodología (Capítulo II) — profundizada
La profesora la veía corta. Se amplió de forma considerable (de ~2.400 a ~10.900 caracteres) **manteniendo tu estilo** y atando cada fase al prototipo real:
- **2.1**: se agregó qué significa la "Q" (aseguramiento de calidad) y por qué un sistema probabilístico exige un proceso iterativo, no lineal.
- **2.2**: un párrafo marco al inicio + se profundizó **cada una de las 6 fases** (Comprensión del negocio, Ingeniería de datos, Ingeniería del modelo, Aseguramiento de calidad, Despliegue, Monitoreo y mantenimiento), explicando las decisiones tomadas y conectándolas con los capítulos siguientes.

## Citas [#] en todo el documento
Se distribuyeron referencias a lo largo de toda la tesis "siempre que se pudo". Ahora **las 20 referencias se citan en el texto** (cada una ≥ 2 veces), repartidas entre Introducción, Metodología (13 referencias distintas), Herramientas, Resultados y Conclusiones.

## Foco y uniformidad
Se releyó el informe completo. Mantiene una sola idea de punta a punta: **un prototipo experimental de RAG aplicado a un sitio web institucional**, recorriendo el ciclo extracción → representación → recuperación → generación → integración. La metodología ahora refuerza ese hilo en lugar de quedar como un anexo teórico, con referencias cruzadas explícitas al Capítulo IV.

> Nota: los números de página de los índices siguen siendo aproximados (el documento creció ~4 páginas con la ampliación). Conviene darles una pasada final antes de imprimir.
