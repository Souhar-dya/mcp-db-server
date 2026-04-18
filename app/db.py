"""
Database management utilities for MCP Database Server

Handles connection management, query execution, and safety checks.
"""

import os
import re
import logging
import ast
from urllib.parse import urlparse
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Conditionally import database drivers
try:
    import asyncpg
except ImportError:
    asyncpg = None

try:
    import pymysql
except ImportError:
    pymysql = None

try:
    import aiosqlite
except ImportError:
    aiosqlite = None

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

from sqlalchemy import create_engine, text, MetaData, inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Manages database connections and operations"""
    
    def __init__(self):
        self.database_url = self._get_database_url()
        self.database_type = self._detect_database_type()
        self.engine = None
        self.mongo_client = None
        self.mongo_database = None
        self.async_session_maker = None
        self._initialize_engine()
    
    def _get_database_url(self) -> str:
        """Get database URL from environment variables"""
        # Try different environment variable patterns
        db_url = (
            os.getenv("DATABASE_URL") or
            os.getenv("DB_URL") or
            os.getenv("POSTGRES_URL") or
            os.getenv("MYSQL_URL") or
            os.getenv("MONGO_URL")
        )
        
        if not db_url:
            # Default to PostgreSQL with common defaults
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5432")
            user = os.getenv("DB_USER", "postgres")
            password = os.getenv("DB_PASSWORD", "postgres")
            database = os.getenv("DB_NAME", "postgres")
            
            db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
        else:
            # Convert standard PostgreSQL URL to async format for compatibility
            if db_url.startswith("postgresql://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif db_url.startswith("mysql://"):
                db_url = db_url.replace("mysql://", "mysql+aiomysql://", 1)
        
        return db_url
    
    def _detect_database_type(self) -> str:
        """Detect database type from URL"""
        if "postgresql" in self.database_url or "postgres" in self.database_url:
            return "postgresql"
        elif "mysql" in self.database_url:
            return "mysql"
        elif "sqlite" in self.database_url:
            return "sqlite"
        elif self.database_url.startswith("mongodb://") or self.database_url.startswith("mongodb+srv://"):
            return "mongodb"
        else:
            return "postgresql"  # Default fallback

    def _get_mongo_database_name(self) -> str:
        """Extract MongoDB database name from URL or environment."""
        parsed = urlparse(self.database_url)
        if parsed.path and parsed.path != "/":
            return parsed.path.lstrip("/")
        return os.getenv("MONGO_DB", "test")

    def _serialize_mongo_value(self, value: Any) -> Any:
        """Convert MongoDB-specific values into JSON-serializable values."""
        if isinstance(value, dict):
            return {k: self._serialize_mongo_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._serialize_mongo_value(v) for v in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if not isinstance(value, (str, int, float, bool, type(None))):
            return str(value)
        return value

    def _split_csv_quoted(self, raw: str) -> List[str]:
        """Split comma-separated values while preserving quoted segments."""
        parts: List[str] = []
        buf: List[str] = []
        quote: Optional[str] = None
        i = 0
        while i < len(raw):
            ch = raw[i]
            if quote:
                buf.append(ch)
                if ch == quote and (i == 0 or raw[i - 1] != "\\"):
                    quote = None
            else:
                if ch in {"'", '"'}:
                    quote = ch
                    buf.append(ch)
                elif ch == ",":
                    parts.append("".join(buf).strip())
                    buf = []
                else:
                    buf.append(ch)
            i += 1
        if buf:
            parts.append("".join(buf).strip())
        return [p for p in parts if p]

    def _parse_sql_like_literal(self, token: str) -> Any:
        """Parse SQL-like literal text into Python values for MongoDB writes."""
        t = token.strip()
        if not t:
            return None

        upper = t.upper()
        if upper == "NULL":
            return None
        if upper == "TRUE":
            return True
        if upper == "FALSE":
            return False

        if (t.startswith("'") and t.endswith("'")) or (t.startswith('"') and t.endswith('"')):
            inner = t[1:-1]
            return inner.replace("\\'", "'").replace('\\"', '"')

        try:
            if "." in t:
                return float(t)
            return int(t)
        except ValueError:
            pass

        return t

    def _parse_where_equals(self, where_clause: str) -> Dict[str, Any]:
        """Parse a minimal SQL WHERE clause of key=value [AND key=value ...]."""
        clause = where_clause.strip()
        if not clause:
            return {}

        filters: Dict[str, Any] = {}
        conditions = re.split(r"\s+AND\s+", clause, flags=re.IGNORECASE)
        for cond in conditions:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", cond.strip())
            if not match:
                raise ValueError(f"Unsupported WHERE condition for MongoDB: {cond}")
            key = match.group(1)
            value = self._parse_sql_like_literal(match.group(2).strip())
            filters[key] = value
        return filters

    async def _execute_unsafe_mongo_query(self, query: str) -> List[Dict[str, Any]]:
        """Execute SQL-like write operations against MongoDB collections."""
        if self.mongo_database is None:
            raise ValueError("MongoDB is not initialized")

        normalized = " ".join(query.strip().rstrip(";").split())

        # CREATE TABLE users (...) -> create collection 'users'
        create_match = re.match(
            r"^CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)$",
            normalized,
            flags=re.IGNORECASE,
        )
        if create_match:
            collection_name = create_match.group(1)
            existing = await self.mongo_database.list_collection_names()
            if collection_name not in existing:
                await self.mongo_database.create_collection(collection_name)
                return [{"affected_rows": 0, "status": "success", "query_type": "modification", "message": f"Collection '{collection_name}' created"}]
            return [{"affected_rows": 0, "status": "success", "query_type": "modification", "message": f"Collection '{collection_name}' already exists"}]

        # DROP TABLE users -> drop collection
        drop_match = re.match(
            r"^DROP\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)$",
            normalized,
            flags=re.IGNORECASE,
        )
        if drop_match:
            collection_name = drop_match.group(1)
            await self.mongo_database[collection_name].drop()
            return [{"affected_rows": 0, "status": "success", "query_type": "modification", "message": f"Collection '{collection_name}' dropped"}]

        # INSERT INTO users (name, age) VALUES ('a', 2)
        insert_match = re.match(
            r"^INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^\)]*)\)\s*VALUES\s*\(([^\)]*)\)$",
            normalized,
            flags=re.IGNORECASE,
        )
        if insert_match:
            collection_name = insert_match.group(1)
            cols = [c.strip() for c in self._split_csv_quoted(insert_match.group(2))]
            vals = [self._parse_sql_like_literal(v) for v in self._split_csv_quoted(insert_match.group(3))]
            if len(cols) != len(vals):
                raise ValueError("INSERT columns and values count mismatch")
            doc = {k: v for k, v in zip(cols, vals)}
            res = await self.mongo_database[collection_name].insert_one(doc)
            return [{"affected_rows": 1, "status": "success", "query_type": "modification", "inserted_id": str(res.inserted_id)}]

        # DELETE FROM users WHERE name='a'
        delete_match = re.match(
            r"^DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+WHERE\s+(.+))?$",
            normalized,
            flags=re.IGNORECASE,
        )
        if delete_match:
            collection_name = delete_match.group(1)
            where_clause = delete_match.group(2) or ""
            filters = self._parse_where_equals(where_clause)
            if filters:
                res = await self.mongo_database[collection_name].delete_many(filters)
            else:
                res = await self.mongo_database[collection_name].delete_many({})
            return [{"affected_rows": res.deleted_count, "status": "success", "query_type": "modification"}]

        # UPDATE users SET age=30, name='x' WHERE name='a'
        update_match = re.match(
            r"^UPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\s+SET\s+(.+?)(?:\s+WHERE\s+(.+))?$",
            normalized,
            flags=re.IGNORECASE,
        )
        if update_match:
            collection_name = update_match.group(1)
            set_clause = update_match.group(2)
            where_clause = update_match.group(3) or ""

            updates: Dict[str, Any] = {}
            assignments = self._split_csv_quoted(set_clause)
            for item in assignments:
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", item.strip())
                if not match:
                    raise ValueError(f"Unsupported SET assignment for MongoDB: {item}")
                key = match.group(1)
                value = self._parse_sql_like_literal(match.group(2))
                updates[key] = value

            filters = self._parse_where_equals(where_clause)
            res = await self.mongo_database[collection_name].update_many(filters, {"$set": updates})
            return [{"affected_rows": res.modified_count, "status": "success", "query_type": "modification"}]

        raise ValueError(
            "Unsupported MongoDB unsafe query format. Supported: CREATE TABLE, DROP TABLE, INSERT INTO, DELETE FROM, UPDATE ... SET ... [WHERE ...]."
        )

    async def _execute_safe_mongo_query(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Execute a restricted SQL-like query against MongoDB collections.

        Supported query forms:
        - SELECT * FROM <collection> [LIMIT n]
        - SELECT COUNT(*) as <alias> FROM <collection>
        """
        if self.mongo_database is None:
            raise ValueError("MongoDB is not initialized")

        normalized = " ".join(query.strip().rstrip(";").split())

        count_pattern = re.compile(
            r"^SELECT\s+COUNT\(\*\)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:LIMIT\s+\d+)?$",
            flags=re.IGNORECASE,
        )
        select_pattern = re.compile(
            r"^SELECT\s+\*\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+LIMIT\s+(\d+))?$",
            flags=re.IGNORECASE,
        )

        count_match = count_pattern.match(normalized)
        if count_match:
            alias = count_match.group(1)
            collection_name = count_match.group(2)
            collection = self.mongo_database[collection_name]
            total = await collection.count_documents({})
            return [{alias: total}]

        select_match = select_pattern.match(normalized)
        if select_match:
            collection_name = select_match.group(1)
            query_limit = int(select_match.group(2)) if select_match.group(2) else limit
            final_limit = min(max(query_limit, 1), limit)
            collection = self.mongo_database[collection_name]
            docs = await collection.find({}).limit(final_limit).to_list(length=final_limit)
            return [self._serialize_mongo_value(doc) for doc in docs]

        raise ValueError(
            "Unsupported MongoDB safe query format. Use 'SELECT * FROM <collection> [LIMIT n]' "
            "or 'SELECT COUNT(*) as <alias> FROM <collection>'."
        )
    
    def _initialize_engine(self):
        """Initialize SQLAlchemy async engine with optional MySQL SSL support"""
        try:
            if self.database_type == "mongodb":
                if AsyncIOMotorClient is None:
                    raise ImportError("motor is required for MongoDB support. Install with: pip install motor")

                timeout_ms = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))
                self.mongo_client = AsyncIOMotorClient(
                    self.database_url,
                    serverSelectionTimeoutMS=timeout_ms,
                )
                self.mongo_database = self.mongo_client[self._get_mongo_database_name()]
                logger.info("MongoDB client initialized")
                return

            connect_args = {}
            # Only for MySQL: add SSL context if required
            if self.database_type == "mysql":
                # Check for ssl_mode or ssl required in the URL or env
                url_lower = self.database_url.lower()
                if (
                    "ssl_mode=required" in url_lower or
                    "ssl-mode=required" in url_lower or
                    os.getenv("MYSQL_SSL", "false").lower() == "true"
                ):
                    import ssl
                    ssl_context = ssl.create_default_context()
                    connect_args["ssl"] = ssl_context
                    logger.info("MySQL SSL context enabled for connection.")
            self.engine = create_async_engine(
                self.database_url,
                poolclass=NullPool,
                echo=False,
                connect_args=connect_args
            )
            logger.info(f"Database engine initialized for {self.database_type}")
        except Exception as e:
            logger.error(f"Failed to initialize database engine: {e}")
            raise
    
    async def test_connection(self) -> bool:
        """Test database connection"""
        try:
            if self.database_type == "mongodb":
                if self.mongo_client is None:
                    return False
                await self.mongo_client.admin.command("ping")
                return True

            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False
    
    async def list_tables(self) -> List[Dict[str, Any]]:
        """List all tables in the database"""
        try:
            if self.database_type == "mongodb":
                collections = await self.mongo_database.list_collection_names()
                tables: List[Dict[str, Any]] = []
                for name in sorted(collections):
                    count = 0
                    try:
                        count = await self.mongo_database[name].count_documents({})
                    except Exception:
                        count = 0
                    tables.append({"table_name": name, "column_count": count})
                return tables

            async with self.engine.begin() as conn:
                if self.database_type == "postgresql":
                    query = text("""
                        SELECT 
                            table_name,
                            (SELECT COUNT(*) FROM information_schema.columns 
                             WHERE table_schema = t.table_schema 
                             AND table_name = t.table_name) as column_count
                        FROM information_schema.tables t
                        WHERE table_schema = 'public'
                        AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """)
                elif self.database_type == "sqlite":
                    query = text("""
                        SELECT 
                            name as table_name,
                            0 as column_count
                        FROM sqlite_master 
                        WHERE type='table' 
                        AND name NOT LIKE 'sqlite_%'
                        ORDER BY name
                    """)
                else:  # MySQL
                    query = text("""
                        SELECT 
                            table_name,
                            (SELECT COUNT(*) FROM information_schema.columns 
                             WHERE table_schema = t.table_schema 
                             AND table_name = t.table_name) as column_count
                        FROM information_schema.tables t
                        WHERE table_schema = DATABASE()
                        AND table_type = 'BASE TABLE'
                        ORDER BY table_name
                    """)
                
                result = await conn.execute(query)
                tables = []
                for row in result:
                    # Use index-based access instead of attribute access for compatibility
                    table_info = {
                        "table_name": row[0],  # First column is table_name
                        "column_count": row[1] if len(row) > 1 else 0  # Second column is column_count
                    }
                    
                    # For SQLite, get actual column count
                    if self.database_type == "sqlite":
                        col_query = text(f"PRAGMA table_info({row[0]})")
                        col_result = await conn.execute(col_query)
                        table_info["column_count"] = len(list(col_result))
                    
                    tables.append(table_info)
                
                return tables
        except Exception as e:
            logger.error(f"Error listing tables: {e}")
            raise
    
    async def describe_table(self, table_name: str) -> List[Dict[str, Any]]:
        """Get column information for a specific table"""
        try:
            if self.database_type == "mongodb":
                collection = self.mongo_database[table_name]
                sample_doc = await collection.find_one()
                if not sample_doc:
                    return [{"column_name": "_id", "data_type": "objectId", "is_nullable": False}]

                columns = []
                for key, value in sample_doc.items():
                    columns.append(
                        {
                            "column_name": key,
                            "data_type": type(value).__name__,
                            "is_nullable": key != "_id",
                        }
                    )
                return columns

            async with self.engine.begin() as conn:
                if self.database_type == "postgresql":
                    query = text("""
                        SELECT 
                            column_name,
                            data_type,
                            is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                        AND table_name = :table_name
                        ORDER BY ordinal_position
                    """)
                elif self.database_type == "sqlite":
                    query = text(f"PRAGMA table_info({table_name})")
                    result = await conn.execute(query)
                    columns = []
                    for row in result:
                        columns.append({
                            "column_name": row[1],  # name column
                            "data_type": row[2],    # type column
                            "is_nullable": not bool(row[3])  # notnull column
                        })
                    return columns
                else:  # MySQL
                    query = text("""
                        SELECT 
                            column_name,
                            data_type,
                            is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = DATABASE()
                        AND table_name = :table_name
                        ORDER BY ordinal_position
                    """)
                
                result = await conn.execute(query, {"table_name": table_name})
                # Use index-based access for compatibility with different SQLAlchemy versions
                return [
                    {
                        "column_name": row[0],  # column_name
                        "data_type": row[1],    # data_type
                        "is_nullable": row[2] == "YES"  # is_nullable
                    }
                    for row in result
                ]
        except Exception as e:
            logger.error(f"Error describing table {table_name}: {e}")
            raise
    
    def _is_query_safe(self, query: str) -> bool:
        """Check if query is safe (read-only operations only)"""
        # Remove comments and normalize whitespace
        cleaned_query = re.sub(r'--.*$', '', query, flags=re.MULTILINE)
        cleaned_query = re.sub(r'/\*.*?\*/', '', cleaned_query, flags=re.DOTALL)
        cleaned_query = ' '.join(cleaned_query.split()).upper()
        
        # Check for dangerous operations
        dangerous_patterns = [
            r'\bDROP\b', r'\bDELETE\b', r'\bINSERT\b', r'\bUPDATE\b',
            r'\bALTER\b', r'\bCREATE\b', r'\bTRUNCATE\b', r'\bREPLACE\b',
            r'\bMERGE\b', r'\bEXEC\b', r'\bEXECUTE\b', r'\bCALL\b'
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, cleaned_query):
                return False
        
        # Must start with SELECT
        if not re.match(r'^\s*SELECT\b', cleaned_query):
            return False
        
        return True
    
    async def execute_safe_query(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Execute a query with safety checks"""
        # Safety checks
        if not self._is_query_safe(query):
            raise ValueError("Query contains unsafe operations. Only SELECT queries are allowed.")

        if self.database_type == "mongodb":
            return await self._execute_safe_mongo_query(query, limit=limit)
        
        # Add/modify LIMIT clause
        query_upper = query.upper()
        if 'LIMIT' in query_upper:
            # Replace existing LIMIT with our limit
            query = re.sub(r'\bLIMIT\s+\d+', f'LIMIT {limit}', query, flags=re.IGNORECASE)
        else:
            # Add LIMIT clause
            query = f"{query.rstrip(';')} LIMIT {limit}"
        
        try:
            async with self.engine.begin() as conn:
                result = await conn.execute(text(query))
                
                # Convert rows to dictionaries
                rows = []
                for row in result:
                    row_dict = {}
                    for i, col in enumerate(result.keys()):
                        value = row[i]
                        # Handle special types that aren't JSON serializable
                        if hasattr(value, 'isoformat'):  # datetime objects
                            value = value.isoformat()
                        elif hasattr(value, '__str__') and not isinstance(value, (str, int, float, bool, type(None))):
                            value = str(value)
                        row_dict[col] = value
                    rows.append(row_dict)
                
                return rows
                
        except Exception as e:
            logger.error(f"Error executing query: {e}")
            raise
    
    async def execute_unsafe_query(self, query: str) -> List[Dict[str, Any]]:
        """Execute any SQL query without safety restrictions (allows CREATE, DELETE, INSERT, etc.)"""
        try:
            if self.database_type == "mongodb":
                return await self._execute_unsafe_mongo_query(query)

            async with self.engine.begin() as conn:
                result = await conn.execute(text(query))

                # Check if this query returns rows
                if result.returns_rows:
                    # Convert rows to dictionaries
                    rows = []
                    for row in result:
                        row_dict = {}
                        for i, col in enumerate(result.keys()):
                            value = row[i]
                            # Handle special types that aren't JSON serializable
                            if hasattr(value, 'isoformat'):  # datetime objects
                                value = value.isoformat()
                            elif hasattr(value, '__str__') and not isinstance(value, (str, int, float, bool, type(None))):
                                value = str(value)
                            row_dict[col] = value
                        rows.append(row_dict)
                    return rows
                else:
                    # For non-SELECT queries (INSERT, UPDATE, CREATE, DELETE, etc.)
                    return [{"affected_rows": result.rowcount, "status": "success", "query_type": "modification"}]

        except Exception as e:
            logger.error(f"Error executing unsafe query: {e}")
            raise

# Global database manager instance
_db_manager: Optional[DatabaseManager] = None

async def get_db_manager() -> DatabaseManager:
    """Dependency to get database manager instance"""
    global _db_manager
    
    if _db_manager is None:
        _db_manager = DatabaseManager()
        
        # Test connection
        if not await _db_manager.test_connection():
            raise Exception("Cannot connect to database")
    
    return _db_manager

async def cleanup_db_manager():
    """Cleanup database manager"""
    global _db_manager
    
    if _db_manager and _db_manager.engine:
        await _db_manager.engine.dispose()
    if _db_manager and _db_manager.mongo_client:
        _db_manager.mongo_client.close()
    if _db_manager:
        _db_manager = None