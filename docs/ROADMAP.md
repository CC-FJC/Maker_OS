# MakerOS — Roadmap inspirado en AppFlowy (estilo Notion)

Objetivo: tomar lo mejor de AppFlowy (vistas de base de datos, paleta de
comandos, portadas/iconos, pizarra, IA local) sin perder la base de
MakerOS: Markdown + git como fuente de verdad, un solo contenedor, datos
que sobreviven a cualquier update.

## Fase A — UX base estilo Notion/AppFlowy
1. **Paleta de comandos (Ctrl+K)**: buscar páginas y acciones (nueva
   tarea/proyecto/nota, ir a secciones), navegación con teclado.
2. **Vista Grid real** para colecciones: columnas ordenables/filtrables
   (título, estado, fecha, responsable, etiquetas) sobre las tarjetas.
3. **Vista Galería**: tarjetas con portada (banner) grande, como AppFlowy.
4. **Selector de vista por colección persistente** (ya hecho) + atajos.

## Fase B — Vistas de base de datos
5. **Calendario unificado**: tareas con `due` + eventos en la misma vista.
6. **Timeline (línea de tiempo)** para proyectos con fechas start/end.
7. Kanban existente: mantener, pulir drag & drop (hecho).

## Fase C — Relaciones y rollups
8. **Progreso de proyecto calculado** en tarjetas (tareas done/total) —
   parcial, completar en grid.
9. **Backlinks / páginas relacionadas** en el lateral de cada página.
10. Asignation cruzada persona ↔ tareas ↔ proyectos en la página de perfil.

## Fase D — Pizarra (whiteboard) ✅
11. Vista **Pizarra** por página con canvas propio (vanilla JS, sin bundle
    externo): rectángulo, elipse, línea/flecha, lápiz libre, texto, mover,
    borrar (Supr), colores del tema. Se guarda como `<pagina>.pizarra.json`
    junto al `.md` (el indexador solo lee `.md`, así que no ensucia el índice).
    Excalidraw real quedó descartado: su bundle prod son 7+ chunks con nombres
    con hash — frágil de vendorizar.

## Fase E — IA local (LM Studio) ✅
12. Búsqueda semántica/asistente ya existe (`/asistente`); añadido:
    - selector de modelo en la propia página del asistente (lista viva
      desde LM Studio, `/asistente/modelos`, cambio con POST admin),
    - notas de reunión → borrador de tareas (extrae checklist con el
      modelo y crea la página en Tareas),
    - **digest semanal automático**: botón en `/asistente` que resume
      documentos recientes + eventos de la semana y guarda
      `wiki/reports/digest-<fecha>.md`.

## Fase F — Pulido ✅
13. Galería visual de plantillas en Admin → Contenido (tarjetas por
    colección, enlace a `/templates/<colección>.md`).
14. Publicar página individual: botón "Publicar" en cada página
    (toggle `public: true` en frontmatter) + vista solo lectura en
    `/public/<colección>/<página>` accesible sin login.

## Reglas
- Todo dato sigue en Markdown/frontmatter; la UI es una capa encima.
- Nada de CDN: vendorizar cualquier librería nueva en `static/vendor/`.
- Español en UI, inglés en código. Sin emojis.
