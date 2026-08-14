def log_audit(db, user_id, action, entity=None, entity_id=None, details=None, username=None):
    db.execute(
        """INSERT INTO audit_logs (user_id, username, action, entity, entity_id, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, username, action, entity, entity_id, details),
    )
    db.commit()
