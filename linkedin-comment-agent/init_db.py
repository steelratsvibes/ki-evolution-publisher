import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comments.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_posts (
            post_urn TEXT PRIMARY KEY,
            text TEXT,
            created_at INTEGER,
            last_checked INTEGER,
            active INTEGER DEFAULT 1
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_comments (
            comment_id TEXT PRIMARY KEY,
            post_urn TEXT,
            author_urn TEXT,
            author_name TEXT,
            text TEXT,
            created_at INTEGER,
            seen_at INTEGER,
            FOREIGN KEY (post_urn) REFERENCES tracked_posts(post_urn)
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS draft_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id TEXT,
            post_urn TEXT,
            original_comment TEXT,
            draft_text TEXT,
            status TEXT DEFAULT 'draft',
            created_at INTEGER,
            approved_at INTEGER,
            posted_at INTEGER,
            feedback TEXT,
            FOREIGN KEY (comment_id) REFERENCES seen_comments(comment_id),
            FOREIGN KEY (post_urn) REFERENCES tracked_posts(post_urn)
        )
    """)
    
    conn.commit()
    conn.close()
    print(f"DB initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()
