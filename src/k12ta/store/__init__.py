"""SQLite persistence, stdlib only, no ORM.

Every table's primary key and every foreign key includes `student_id`, so a row can
never reference a parent row belonging to a different student — SQLite rejects the
insert. See docs/ARCHITECTURE.md, "Multi-user".
"""
