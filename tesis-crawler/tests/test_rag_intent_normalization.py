from app.core.rag_service import RAGService
from app.core.ingestion_service import IngestionService


def test_admissions_typos_are_normalized() -> None:
    fixed = RAGService._normalize_query_typos("Perfecto, y cuando son las adminiciones?")
    assert "admisiones" in fixed.lower()


def test_inscribirme_a_la_carrera_adds_admissions_hints() -> None:
    resolved = RAGService._normalize_query_for_intent("que necesito para inscribirme a la carrera?")
    low = resolved.lower()
    assert "inscripcion" in low
    assert "admision" in low
    assert "ingreso" in low


def test_admissions_detection_accepts_common_variants() -> None:
    assert RAGService._is_admissions_query("Perfecto, y cuando son las adminiciones?")
    assert RAGService._is_admissions_query("Que necesito para inscribirme a la facultad?")
    assert RAGService._is_admissions_query("Quiero anotarme y matricularme")


def test_admissions_query_is_not_classified_as_subjects() -> None:
    q = "que necesito para inscribirme a la facultad?"
    assert RAGService._is_admissions_query(q)
    assert not RAGService._is_year_subjects_query(q)


def test_extract_subjects_uses_best_year_block_when_year_labels_repeat() -> None:
    content = """
Indice rapido
Primer Año
Segundo Año
Tercer Año
Cuarto Año

Tercer Año
PRIMER SEMESTRE
Materia:
Medicina I
Carga Horaria:
400 horas
SEGUNDO SEMESTRE
Materia:
Emergentología
Materia:
SALUD PÚBLICA
Materia:
DIAGNÓSTICO POR IMÁGENES
Materia:
SALUD MENTAL Y PSIQUIATRÍA
Materia:
SEXOLOGÍA (Optativa)
Cuarto Año
"""
    subjects = RAGService._extract_subjects_from_year_block(content, 3)
    low = [s.lower() for s in subjects]
    assert "medicina i" in low
    assert "emergentología" in low
    assert "salud mental y psiquiatría" in low


def test_ingestion_extracts_year_subjects_from_full_curriculum_block() -> None:
    content = """
Indice rapido
Primer Año
Segundo Año
Tercer Año
Cuarto Año

Tercer Año
PRIMER SEMESTRE
Materia:
Medicina I
SEGUNDO SEMESTRE
Materia:
Emergentología
Materia:
SALUD PÚBLICA
Materia:
DIAGNÓSTICO POR IMÁGENES
Materia:
HOMEÓSTASIS DEL MEDIO INTERNO (Optativa)
Cuarto Año
"""
    facts = IngestionService._extract_year_subject_facts("https://med.unne.edu.ar/carreras/medicina", content)
    year3 = [f["fact_value"].lower() for f in facts if f.get("fact_key") == "year_3_subject"]
    assert "medicina i" in year3
    assert "emergentología" in year3
    assert "diagnóstico por imágenes" in year3


def test_ingestion_does_not_extract_year_subjects_from_pdf_sources() -> None:
    content = """
Primer Año
Materia:
Medicina, Hombre y Sociedad
Segundo Año
Materia:
FISIOLOGÍA HUMANA
"""
    facts = IngestionService._extract_program_facts(
        "https://med.unne.edu.ar/wp-content/uploads/2024/02/RES-2023-239-CD-MEDUNNE-Programa-Clinica-Gineco.pdf",
        "Programa Clinica Gineco",
        content,
        page_type="curriculum",
    )
    assert not any(str(f.get("fact_key", "")).startswith("year_") for f in facts)


def test_program_slug_matching_uses_url_not_generic_faculty_word() -> None:
    slugs = RAGService._slug_candidates_for_program("Medicina")
    assert RAGService._url_matches_program_slugs("https://med.unne.edu.ar/carreras/medicina", slugs)
    assert not RAGService._url_matches_program_slugs(
        "https://med.unne.edu.ar/carreras/licenciatura-en-enfermeria/distribucion-de-asignaturas",
        slugs,
    )


def test_program_doc_relevance_penalizes_other_career_slug() -> None:
    slugs = RAGService._slug_candidates_for_program("Medicina")
    variants = RAGService._program_lookup_variants("Medicina")

    med_score = RAGService._program_doc_relevance_score(
        url="https://med.unne.edu.ar/carreras/medicina",
        title="Medicina Facultad de Medicina",
        text_value="Plan de estudios. Primer año. Medicina, Hombre y Sociedad.",
        slugs=slugs,
        program_variants=variants,
    )
    enf_score = RAGService._program_doc_relevance_score(
        url="https://med.unne.edu.ar/carreras/licenciatura-en-enfermeria/distribucion-de-asignaturas",
        title="Licenciatura en Enfermería - Facultad de Medicina",
        text_value="Materias de enfermería.",
        slugs=slugs,
        program_variants=variants,
    )
    assert med_score > enf_score
    assert enf_score < 40
