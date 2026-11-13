-- More runner families (README "Projects"): node (JavaScript and TypeScript), rust, java (Maven) and
-- godot (GDScript with GUT), alongside python. javascript and typescript stay valid. Idempotent.
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_language_check;
ALTER TABLE projects ADD CONSTRAINT projects_language_check
    CHECK (language IN ('python', 'node', 'javascript', 'typescript', 'rust', 'java', 'godot'));
