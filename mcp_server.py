#!/usr/bin/env python3
"""
MCP Database Server using FastMCP

This is a proper MCP server that Claude can connect to directly.
It provides database query capabilities through the Model Context Protocol.
"""

import asyncio
import os
import sys
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Any

# Add the app directory to the path
# Use absolute path resolution that works both locally and in Docker
_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir / 'app'
if _app_dir.exists():
    sys.path.insert(0, str(_app_dir))
else:
    # Fallback for Docker container where app might be at /app/app
    sys.path.insert(0, '/app/app')

from mcp.server import fastmcp
from dotenv import load_dotenv

# Import our database functionality
from db import DatabaseManager
from nl_to_sql import NLToSQLConverter

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-database-server")

# Load environment variables
load_dotenv()

# Initialize the MCP server
mcp = fastmcp.FastMCP("Database Server")

# Global variables for database connection
db_manager: DatabaseManager = None
nl_converter: NLToSQLConverter = None

@mcp.tool()
async def query_database(query: str) -> str:
    """
    Execute a natural language query on the database.
    
    Args:
        query: Natural language description of what you want to query
        
    Returns:
        The query results formatted as text
    """
    try:
        # Get table schemas for context
        tables = await db_manager.list_tables()
        table_schemas = {}
        for table in tables:
            columns = await db_manager.describe_table(table['table_name'])
            table_schemas[table['table_name']] = columns
        
        # Convert natural language to SQL
        sql_query = nl_converter.convert_to_sql(query, table_schemas)
        
        # Execute the query
        results = await db_manager.execute_safe_query(sql_query)
        
        # Format the response
        response = f"SQL Query: {sql_query}\n\n"
        response += f"Results ({len(results)} rows):\n"
        
        if results:
            # Format as a simple table
            if isinstance(results[0], dict):
                headers = list(results[0].keys())
                response += " | ".join(headers) + "\n"
                response += " | ".join(["-" * len(h) for h in headers]) + "\n"
                
                for row in results[:20]:  # Limit to first 20 rows
                    values = [str(row.get(h, "")) for h in headers]
                    response += " | ".join(values) + "\n"
                
                if len(results) > 20:
                    response += f"\n... and {len(results) - 20} more rows"
            else:
                response += str(results)
        else:
            response += "No results found."
            
        return response
        
    except Exception as e:
        return f"Error executing query: {str(e)}"

@mcp.tool()
async def list_tables() -> str:
    """
    List all tables in the database.
    
    Returns:
        A list of all available tables with their column counts
    """
    try:
        tables = await db_manager.list_tables()
        
        response = "Available Tables:\n\n"
        for table in tables:
            response += f"- {table['table_name']} ({table['column_count']} columns)\n"
        
        return response
        
    except Exception as e:
        return f"Error listing tables: {str(e)}"

@mcp.tool()
async def describe_table(table_name: str) -> str:
    """
    Get the schema/structure of a specific database table.
    
    Args:
        table_name: Name of the table to describe
        
    Returns:
        The table schema with column names, types, and constraints
    """
    try:
        columns = await db_manager.describe_table(table_name)
        
        response = f"Table: {table_name}\n\n"
        response += "Columns:\n"
        for col in columns:
            nullable = "NULL" if col['is_nullable'] else "NOT NULL"
            response += f"- {col['column_name']}: {col['data_type']} ({nullable})\n"
        
        return response
        
    except Exception as e:
        return f"Error describing table: {str(e)}"

@mcp.tool()
async def execute_sql(sql_query: str) -> str:
    """
    Execute a raw SQL query on the database.
    
    Args:
        sql_query: The SQL query to execute
        
    Returns:
        The query results formatted as text
    """
    try:
        # Basic safety check for destructive operations
        sql_lower = sql_query.lower().strip()
        dangerous_keywords = ['drop', 'delete', 'truncate', 'alter', 'update']
        
        for keyword in dangerous_keywords:
            if sql_lower.startswith(keyword):
                return f"Error: {keyword.upper()} operations are not allowed for safety reasons."
        
        # Execute the query
        results = await db_manager.execute_safe_query(sql_query)
        
        # Format the response
        response = f"SQL Query: {sql_query}\n\n"
        response += f"Results ({len(results)} rows):\n"
        
        if results:
            # Format as a simple table
            if isinstance(results[0], dict):
                headers = list(results[0].keys())
                response += " | ".join(headers) + "\n"
                response += " | ".join(["-" * len(h) for h in headers]) + "\n"
                
                for row in results[:20]:  # Limit to first 20 rows
                    values = [str(row.get(h, "")) for h in headers]
                    response += " | ".join(values) + "\n"
                
                if len(results) > 20:
                    response += f"\n... and {len(results) - 20} more rows"
            else:
                response += str(results)
        else:
            response += "No results found."
            
        return response
        
    except Exception as e:
        return f"Error executing SQL: {str(e)}"

@mcp.tool()
async def connect_to_database(database_url: str) -> str:
    """
    Connect to a different database dynamically.
    
    Args:
        database_url: Database connection URL (e.g., sqlite+aiosqlite:///path/to/db.db, 
                     postgresql+asyncpg://user:pass@host:port/db)
    
    Returns:
        Connection status and available tables
    """
    global db_manager
    
    try:
        # Validate URL format
        supported_types = ['sqlite+aiosqlite://', 'postgresql+asyncpg://', 'mysql+aiomysql://']
        if not any(database_url.startswith(db_type) for db_type in supported_types):
            return f"ERROR: Unsupported database URL format. Supported: {', '.join(supported_types)}"
        
        # Store current URL and update environment
        previous_url = os.getenv('DATABASE_URL', 'None')
        os.environ['DATABASE_URL'] = database_url
        
        # Create new database manager
        new_db_manager = DatabaseManager()
        
        # Test the connection
        connection_test = await new_db_manager.test_connection()
        if not connection_test:
            # Restore previous URL on failure
            if previous_url != 'None':
                os.environ['DATABASE_URL'] = previous_url
            return f"Failed to connect to database: {database_url}"
        
        # If successful, update the global manager
        db_manager = new_db_manager
        
        # Get table information
        tables = await db_manager.list_tables()
        table_info = [f"  - {table['table_name']} ({table['column_count']} columns)" for table in tables]
        
        response = f"Successfully connected to database!\n"
        response += f" Database URL: {database_url}\n"
        response += f" Database Type: {db_manager.database_type}\n"
        response += f" Available Tables ({len(tables)}):\n"
        response += "\n".join(table_info) if table_info else "  No tables found"
        
        logger.info(f"Dynamic database connection successful: {database_url}")
        return response
        
    except Exception as e:
        # Restore previous URL on error
        if 'previous_url' in locals() and previous_url != 'None':
            os.environ['DATABASE_URL'] = previous_url
        
        error_msg = f" Error connecting to database: {str(e)}"
        logger.error(f"Dynamic database connection failed: {error_msg}")
        return error_msg

@mcp.tool()
async def get_connection_examples() -> str:
    """
    Get examples of database connection URLs for different database types.
    
    Returns:
        Examples of connection strings for various databases
    """
    examples = """
**Database Connection Examples:**

**SQLite (Local Files):**
• sqlite+aiosqlite:///students.db
• sqlite+aiosqlite:///C:/path/to/database.db
• sqlite+aiosqlite:///./local_database.db

**PostgreSQL:**
• postgresql+asyncpg://username:password@localhost:5432/database_name
• postgresql+asyncpg://user:pass@myserver.com:5432/students_db
• postgresql+asyncpg://postgres:admin123@127.0.0.1:5432/school

**MySQL:**
• mysql+aiomysql://username:password@localhost:3306/database_name
• mysql+aiomysql://user:pass@mysql-server:3306/students_db

**Cloud Databases:**
• postgresql+asyncpg://user:pass@host.region.rds.amazonaws.com:5432/db
• postgresql+asyncpg://user:pass@host.supabase.co:5432/postgres

**Tips:**
- Replace 'username', 'password', 'host', 'port', 'database_name' with actual values
- Use URL encoding for special characters in passwords
- SQLite files can be relative or absolute paths
- Test connection with a simple database first
"""
    return examples

@mcp.tool()
async def get_current_database_info() -> str:
    """
    Get information about the currently connected database.
    
    Returns:
        Current database connection details and statistics
    """
    try:
        if not db_manager:
            return "ERROR: No database connection established"
        
        # Test current connection
        is_connected = await db_manager.test_connection()
        if not is_connected:
            return "ERROR: Current database connection is not working"
        
        # Get database info
        current_url = os.getenv('DATABASE_URL', 'Unknown')
        tables = await db_manager.list_tables()
        
        # Calculate some statistics
        total_tables = len(tables)
        total_columns = sum(table['column_count'] for table in tables)
        
        response = f"**Current Database Information:**\n\n"
        response += f"**Connection URL:** {current_url}\n"
        response += f"**Database Type:** {db_manager.database_type}\n"
        response += f"**Status:** Connected\n"
        response += f"**Tables:** {total_tables}\n"
        response += f"**Total Columns:** {total_columns}\n\n"
        
        if tables:
            response += "**Table Details:**\n"
            for table in tables:
                response += f"  • {table['table_name']} ({table['column_count']} columns)\n"
        else:
            response += "**No tables found in database**\n"
        
        return response
        
    except Exception as e:
        return f"ERROR: Error getting database info: {str(e)}"

@mcp.tool()
async def execute_unsafe_sql(sql_query: str) -> str:
    """
    Execute any SQL query without safety restrictions (allows CREATE, DELETE, INSERT, DROP, etc.).
    WARNING: This tool can modify or delete data. Use with caution!
    
    Args:
        sql_query: Any SQL query including CREATE, INSERT, UPDATE, DELETE, DROP operations
        
    Returns:
        The query results or success message for modification operations
    """
    try:
        results = await db_manager.execute_unsafe_query(sql_query)
        
        if not results:
            return "Query executed successfully (no results returned)"
        
        # Check if this is a modification operation
        if len(results) == 1 and 'query_type' in results[0] and results[0]['query_type'] == 'modification':
            affected_rows = results[0].get('affected_rows', 0)
            return f"Query executed successfully. Affected rows: {affected_rows}"
        
        # Format results for display
        if isinstance(results, list) and len(results) > 0:
            response = f"Query Results ({len(results)} rows):\n\n"
            for i, row in enumerate(results):
                response += f"Row {i+1}:\n"
                for key, value in row.items():
                    response += f"  {key}: {value}\n"
                response += "\n"
            return response
        else:
            return "Query executed successfully (no data returned)"
            
    except Exception as e:
        return f"ERROR: Error executing unsafe SQL: {str(e)}"

@mcp.tool()
async def create_table(table_name: str, columns_definition: str) -> str:
    """
    Create a new table in the database.
    
    Args:
        table_name: Name of the table to create
        columns_definition: Column definitions (e.g., "id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER")
        
    Returns:
        Success message or error details
    """
    try:
        create_query = f"CREATE TABLE {table_name} ({columns_definition})"
        results = await db_manager.execute_unsafe_query(create_query)
        return f"Table '{table_name}' created successfully!"
        
    except Exception as e:
        return f"ERROR: Error creating table: {str(e)}"

@mcp.tool()
async def insert_data(table_name: str, columns: str, values: str) -> str:
    """
    Insert data into a table.
    
    Args:
        table_name: Name of the table to insert into
        columns: Column names (e.g., "name, age, email")
        values: Values to insert (e.g., "'John Doe', 30, 'john@example.com'")
        
    Returns:
        Success message with number of rows affected
    """
    try:
        insert_query = f"INSERT INTO {table_name} ({columns}) VALUES ({values})"
        results = await db_manager.execute_unsafe_query(insert_query)
        affected_rows = results[0].get('affected_rows', 0) if results else 0
        return f"Inserted {affected_rows} row(s) into '{table_name}'"
        
    except Exception as e:
        return f"ERROR: Error inserting data: {str(e)}"

@mcp.tool()
async def delete_data(table_name: str, where_condition: str = "") -> str:
    """
    Delete data from a table.
    WARNING: This will permanently delete data!
    
    Args:
        table_name: Name of the table to delete from
        where_condition: WHERE clause condition (e.g., "id = 1" or "age > 65"). If empty, ALL rows will be deleted!
        
    Returns:
        Success message with number of rows deleted
    """
    try:
        if where_condition.strip():
            delete_query = f"DELETE FROM {table_name} WHERE {where_condition}"
        else:
            delete_query = f"DELETE FROM {table_name}"
            
        results = await db_manager.execute_unsafe_query(delete_query)
        affected_rows = results[0].get('affected_rows', 0) if results else 0
        return f"Deleted {affected_rows} row(s) from '{table_name}'"
        
    except Exception as e:
        return f"ERROR: Error deleting data: {str(e)}"

@mcp.tool()
async def update_data(table_name: str, set_clause: str, where_condition: str = "") -> str:
    """
    Update data in a table.
    
    Args:
        table_name: Name of the table to update
        set_clause: SET clause (e.g., "name = 'New Name', age = 25")
        where_condition: WHERE clause condition (e.g., "id = 1"). If empty, ALL rows will be updated!
        
    Returns:
        Success message with number of rows updated
    """
    try:
        if where_condition.strip():
            update_query = f"UPDATE {table_name} SET {set_clause} WHERE {where_condition}"
        else:
            update_query = f"UPDATE {table_name} SET {set_clause}"
            
        results = await db_manager.execute_unsafe_query(update_query)
        affected_rows = results[0].get('affected_rows', 0) if results else 0
        return f"Updated {affected_rows} row(s) in '{table_name}'"
        
    except Exception as e:
        return f"ERROR: Error updating data: {str(e)}"

@mcp.resource("database://tables")
async def get_database_tables() -> str:
    """Resource that provides information about database tables"""
    tables = await db_manager.list_tables()
    table_list = "\n".join([f"- {table['table_name']} ({table['column_count']} columns)" for table in tables])
    return f"Available Tables:\n{table_list}"

@mcp.resource("database://schema")
async def get_database_schema() -> str:
    """Resource that provides the complete database schema"""
    tables = await db_manager.list_tables()
    schema_info = {}
    for table in tables:
        columns = await db_manager.describe_table(table['table_name'])
        schema_info[table['table_name']] = columns
    
    import json
    return json.dumps(schema_info, indent=2)

async def initialize_database(database_url: str = None, config_file: str = None):
    """Initialize the database connection"""
    global db_manager, nl_converter
    
    try:
        # Determine database URL from various sources
        final_database_url = None
        
        # Priority: command line URL > config file > environment variable
        if database_url:
            final_database_url = database_url
            logger.info("Using database URL from command line argument")
        elif config_file:
            # Simple JSON config file support
            try:
                import json
                with open(config_file, 'r') as f:
                    config = json.load(f)
                final_database_url = config.get('database_url')
                if not final_database_url:
                    raise ValueError("database_url not found in config file")
                logger.info(f"Using database URL from config file: {config_file}")
            except Exception as e:
                logger.error(f"Failed to load config file {config_file}: {e}")
                raise
        else:
            final_database_url = os.getenv('DATABASE_URL')
            if final_database_url:
                logger.info("Using database URL from environment variable")
        
        if not final_database_url:
            raise ValueError("No DATABASE_URL provided via argument, config file, or environment variable")
        
        # Set the environment variable for DatabaseManager
        os.environ['DATABASE_URL'] = final_database_url
        
        db_manager = DatabaseManager()  # No parameter needed
        await db_manager.test_connection()
        logger.info(f"Connected to database: {final_database_url}")
        
        # Initialize NL to SQL converter
        nl_converter = NLToSQLConverter()
        logger.info("NL to SQL converter initialized")
        
        logger.info("Database initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

async def main(database_url: str = None, config_file: str = None):
    """Main function to run the MCP server"""
    
    # Initialize database connection
    await initialize_database(database_url, config_file)
    
    # Run the MCP server using stdio
    await mcp.run_stdio_async()

if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='MCP Database Server')
    parser.add_argument('--database-url', '-d', 
                       help='Database URL (e.g., sqlite+aiosqlite:///students.db or postgresql+asyncpg://user:pass@host:port/db)')
    parser.add_argument('--config-file', '-c',
                       help='Path to configuration file with database settings')
    
    args = parser.parse_args()
    
    async def run_server():
        await initialize_database(args.database_url, args.config_file)
        await mcp.run_stdio_async()
    
    asyncio.run(run_server())