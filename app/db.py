"""
Database management utilities for MCP Database Server

Handles connection management, query execution, and safety checks.
"""

import os
import re
import logging
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
        self.async_session_maker = None
        self._initialize_engine()
    
    def _get_database_url(self) -> str:
        """Get database URL from environment variables"""
        # Try different environment variable patterns
        db_url = (
            os.getenv("DATABASE_URL") or
            os.getenv("DB_URL") or
            os.getenv("POSTGRES_URL") or
            os.getenv("MYSQL_URL")
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
        else:
            return "postgresql"  # Default fallback
    
    def _initialize_engine(self):
        """Initialize SQLAlchemy async engine"""
        try:
            self.engine = create_async_engine(
                self.database_url,
                poolclass=NullPool,
                echo=False
            )
            logger.info(f"Database engine initialized for {self.database_type}")
        except Exception as e:
            logger.error(f"Failed to initialize database engine: {e}")
            raise
    
    async def test_connection(self) -> bool:
        """Test database connection"""
        try:
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False
    
    async def list_tables(self) -> List[Dict[str, Any]]:
        """List all tables in the database"""
        try:
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
        _db_manager = None