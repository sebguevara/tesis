# Análisis de la Extensión `pgai` en el Proyecto

Este documento detalla el funcionamiento, implementación y ventajas de utilizar la extensión `pgai` de PostgreSQL en el contexto de este proyecto (`tesis-crawler`).

## 1. ¿Qué es `pgai`?

`pgai` es una extensión de PostgreSQL desarrollada por Timescale que integra capacidades de Inteligencia Artificial directamente dentro de la base de datos. Su función principal es simplificar el flujo de trabajo de RAG (Retrieval Augmented Generation) al manejar la generación, almacenamiento y búsqueda de embeddings vectoriales de forma automática y transparente.

En lugar de gestionar embeddings en el código de la aplicación (Python) o usar bases de datos vectoriales separadas (como Pinecone o Weaviate), `pgai` permite definir "vectorizadores" que operan sobre tablas existentes.

## 2. Comparación: Enfoque Tradicional vs. `pgai`

A continuación, se comparan las diferencias clave entre un flujo de trabajo tradicional y el implementado con `pgai`.

| Característica               | Enfoque Tradicional                                                                     | Enfoque Moderno (`pgai`)                                                               |
| :--------------------------- | :-------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------- |
| **Generación de Embeddings** | Gestionada por la aplicación (llamadas a la API de OpenAI en Python).                   | Gestionada por la base de datos (worker en segundo plano).                             |
| **Sincronización**           | Compleja. Si se actualiza un registro, el código debe recordar actualizar el embedding. | Automática. `pgai` detecta cambios (INSERT/UPDATE) y regenera embeddings.              |
| **Búsqueda Vectorial**       | Requiere pasos manuales: generar embedding de la query en Python -> enviar a DB.        | Integrada en SQL. Se llama a `ai.openai_embed` directamente en la cláusula `ORDER BY`. |
| **Consistencia de Datos**    | Riesgo de desincronización entre el texto y su vector.                                  | Garantizada. El vector siempre corresponde al texto almacenado.                        |
| **Infraestructura**          | Requiere lógica de colas o workers adicionales para procesar documentos grandes.        | Utiliza un contenedor `vectorizer-worker` dedicado que escala independientemente.      |

## 3. Implementación en el Proyecto

El proyecto utiliza `pgai` para vectorizar automáticamente el contenido de la tabla `documents`.

### A. Configuración y Creación del Vectorizador

En `app/storage/db_client.py`, se define la configuración del vectorizador. Esto le indica a `pgai` qué modelo usar y cómo dividir (chunking) el texto.

```python
# app/storage/db_client.py

await conn.execute(text("""
    SELECT ai.create_vectorizer(
        'public.documents'::regclass,                  -- Tabla fuente
        loading       => ai.loading_column('content'), -- Columna a vectorizar
        embedding     => ai.embedding_openai('text-embedding-3-large', 1536), -- Modelo y dimensiones
        chunking      => ai.chunking_character_text_splitter(1500, 200),      -- Estrategia de chunking
        formatting    => ai.formatting_python_template('$chunk'),
        enqueue_existing => true,
        if_not_exists => true
    );
"""))
```

**Análisis:**

- **`chunking`**: Se configura para dividir el texto en bloques de 1500 caracteres con 200 de solapamiento automáticamente, eliminando esta lógica del código Python.
- **`embedding`**: Usa el modelo `text-embedding-3-large` de OpenAI.

### B. Ingesta de Documentos Simplificada

Gracias a `pgai`, el servicio de ingestión (`app/core/ingestion_service.py`) solo necesita guardar el texto limpio en la base de datos. No hay código para llamar a la API de embeddings ni para gestionar vectores.

```python
# app/core/ingestion_service.py

# Simplemente se guarda el contenido.
doc.content = clean_content
session.add(doc)
await session.commit()
# El vectorizador de pgai detectará este cambio y generará los embeddings asíncronamente.
```

### C. Recuperación y Búsqueda (RAG)

En `app/core/rag_service.py`, la búsqueda se realiza mediante consultas SQL que aprovechan las funciones de `pgai`.

1. **Resolución Dinámica de Tablas**:
   `pgai` crea tablas internas para gestionar los chunks y embeddings. El método `_resolve_embeddings_relation` consulta las vistas de sistema de `pgai` para encontrar dónde están los datos:

   ```python
   # Determina dinámicamente dónde pgai guardó los vectores para la tabla 'documents'
   vectorizer_id = await session.execute(
       text("SELECT id FROM ai.vectorizer WHERE source_table = 'public.documents'::regclass")
   )
   # ... lógica para obtener nombres de tablas de embeddings ...
   ```

2. **Búsqueda Vectorial en SQL**:
   La consulta de búsqueda genera el embedding de la pregunta del usuario _dentro_ de la misma consulta SQL, asegurando que se use el mismo modelo que se usó para indexar los documentos.

   ```sql
   ORDER BY de.embedding <=> ai.openai_embed('text-embedding-3-large', :resolved_query, dimensions => 1536)
   LIMIT :k
   ```

   _Nota: `ai.openai_embed` llama a la API de OpenAI desde la base de datos para vectorizar la pregunta `:resolved_query`._

## 4. Ventajas Clave para el Proyecto

1.  **Código más Limpio y Mantenible**:
    - Se elimina gran parte de la complejidad de `ingestion_service.py` y `embeddings_store.py` (que ahora está vacío/obsoleto).
    - La lógica de "cómo vectorizar" está centralizada en la definición de la base de datos, no dispersa en el código.

2.  **Latencia y Rendimiento**:
    - **Data Locality**: Los datos y los vectores viven juntos. PostgreSQL optimiza el acceso a ambos.
    - **Procesamiento Asíncrono**: El usuario no espera a que se generen los embeddings al guardar un documento; el `vectorizer-worker` lo hace en segundo plano.

3.  **Escalabilidad**:
    - Al usar la imagen `timescale/pgai-vectorizer-worker` en Docker, el trabajo pesado de generar embeddings se descarga del contenedor principal de la API (`tesis-crawler`), permitiendo que la API siga respondiendo rápido incluso durante cargas masivas de documentos.

4.  **Flexibilidad**:
    - Cambiar el modelo de embeddings o la estrategia de chunking se puede hacer modificando la definición del vectorizador en la DB, sin necesariamente redesplegar toda la aplicación.
