"""
Natural Language to SQL Converter

Uses HuggingFace transformers to convert natural language queries to SQL.
"""

import os
import logging
import re
from typing import Dict, List, Any, Optional

try:
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, pipeline
    HF_AVAILABLE = True
except (ImportError, AttributeError, RuntimeError) as e:
    HF_AVAILABLE = False
    # Mock classes for when transformers is not available
    class AutoModelForSeq2SeqLM:
        pass
    class AutoTokenizer:
        pass
    def pipeline(*args, **kwargs):
        return None

logger = logging.getLogger(__name__)

class NLToSQLConverter:
    """Converts natural language queries to SQL using HuggingFace models"""
    
    def __init__(self, model_name: str = "gaussalgo/T5-LM-Large-text2sql-spider"):
        self.model_name = model_name
        self.model = None
        self.tokenizer = None
        self.pipeline = None
        self._initialize_model()
    
    def _initialize_model(self):
        """Initialize the HuggingFace model"""
        if not HF_AVAILABLE:
            logger.warning("HuggingFace transformers not available. Using rule-based fallback.")
            return

        # Default disabled to avoid model downloads in low-connectivity environments.
        enable_hf = os.getenv("ENABLE_HF_MODEL", "false").lower() in {"1", "true", "yes", "on"}
        if not enable_hf:
            logger.info("ENABLE_HF_MODEL is disabled. Using rule-based SQL generation.")
            return

        # Default true to avoid network fetches unless explicitly allowed.
        local_files_only = os.getenv("HF_LOCAL_FILES_ONLY", "true").lower() in {"1", "true", "yes", "on"}
        cache_dir = os.getenv("HF_MODEL_CACHE_DIR") or None

        try:
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_name,
                local_files_only=local_files_only,
                cache_dir=cache_dir,
            )
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                self.model_name,
                local_files_only=local_files_only,
                cache_dir=cache_dir,
            )
            self.pipeline = pipeline(
                "text2text-generation",
                model=self.model,
                tokenizer=self.tokenizer,
            )
            logger.info("HuggingFace text2sql model loaded: %s", self.model_name)
        except Exception as e:
            self.model = None
            self.tokenizer = None
            self.pipeline = None
            logger.warning("Failed to load HuggingFace model (%s). Falling back to rule-based SQL.", e)
            return
    
    def _create_table_context(self, table_schemas: Dict[str, List[Dict[str, Any]]]) -> str:
        """Create table context string for the model"""
        context_parts = []
        
        for table_name, columns in table_schemas.items():
            column_info = []
            for col in columns:
                col_str = f"{col['column_name']} {col['data_type']}"
                if not col['is_nullable']:
                    col_str += " NOT NULL"
                column_info.append(col_str)
            
            context_parts.append(f"Table {table_name}: {', '.join(column_info)}")
        
        return " | ".join(context_parts)
    
    def _generate_with_pipeline(self, prompt: str) -> str:
        """Generate SQL using the pipeline"""
        try:
            result = self.pipeline(prompt, max_length=512, num_return_sequences=1)
            return result[0]['generated_text'].strip()
        except Exception as e:
            logger.error(f"Error generating with pipeline: {e}")
            raise
    
    def _generate_with_model(self, prompt: str) -> str:
        """Generate SQL using model directly"""
        try:
            inputs = self.tokenizer.encode(prompt, return_tensors="pt", max_length=512, truncation=True)
            
            with self.tokenizer.as_target_tokenizer():
                outputs = self.model.generate(
                    inputs,
                    max_length=150,
                    num_beams=4,
                    num_return_sequences=1,
                    temperature=0.7,
                    do_sample=False,
                    early_stopping=True
                )
            
            sql = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
            return sql.strip()
        except Exception as e:
            logger.error(f"Error generating with model: {e}")
            raise
    
    def _rule_based_fallback(self, nl_query: str, table_schemas: Dict[str, List[Dict[str, Any]]]) -> str:
        """Rule-based fallback for SQL generation when ML model is not available"""
        nl_lower = nl_query.lower()
        
        # Get first table as default
        table_names = list(table_schemas.keys())
        if not table_names:
            raise ValueError("No tables available")
        
        default_table = table_names[0]
        
        # Simple patterns for common queries
        if any(word in nl_lower for word in ['all', 'everything', 'list', 'show']):
            if 'customers' in nl_lower or 'customer' in nl_lower:
                table = 'customers' if 'customers' in table_names else default_table
            elif 'orders' in nl_lower or 'order' in nl_lower:
                table = 'orders' if 'orders' in table_names else default_table
            else:
                table = default_table
            return f"SELECT * FROM {table}"
        
        if 'count' in nl_lower:
            if 'customers' in nl_lower:
                table = 'customers' if 'customers' in table_names else default_table
            elif 'orders' in nl_lower:
                table = 'orders' if 'orders' in table_names else default_table
            else:
                table = default_table
            return f"SELECT COUNT(*) as count FROM {table}"
        
        if any(word in nl_lower for word in ['top', 'first', 'limit']):
            # Extract number if present
            numbers = re.findall(r'\d+', nl_query)
            limit = numbers[0] if numbers else "10"
            
            if 'customers' in nl_lower:
                table = 'customers' if 'customers' in table_names else default_table
            elif 'orders' in nl_lower:
                table = 'orders' if 'orders' in table_names else default_table
            else:
                table = default_table
            
            return f"SELECT * FROM {table} LIMIT {limit}"
        
        # Default fallback
        return f"SELECT * FROM {default_table}"
    
    def convert_to_sql(self, nl_query: str, table_schemas: Dict[str, List[Dict[str, Any]]]) -> str:
        """Convert natural language query to SQL"""
        try:
            # Create table context
            table_context = self._create_table_context(table_schemas)
            
            # Try ML-based conversion first
            if self.pipeline or (self.model and self.tokenizer):
                try:
                    # Format prompt for the model
                    prompt = f"Tables: {table_context} | Question: {nl_query} | SQL:"
                    
                    if self.pipeline:
                        sql = self._generate_with_pipeline(prompt)
                    else:
                        sql = self._generate_with_model(prompt)
                    
                    # Clean up the generated SQL
                    sql = self._clean_generated_sql(sql)
                    
                    if sql and self._is_valid_sql(sql):
                        logger.info(f"Generated SQL: {sql}")
                        return sql
                    else:
                        logger.warning("Generated SQL is invalid, falling back to rule-based")
                        
                except Exception as e:
                    logger.error(f"ML-based conversion failed: {e}")
            
            # Fallback to rule-based approach
            sql = self._rule_based_fallback(nl_query, table_schemas)
            logger.info(f"Rule-based SQL: {sql}")
            return sql
            
        except Exception as e:
            logger.error(f"Error converting NL to SQL: {e}")
            raise ValueError(f"Failed to convert query to SQL: {str(e)}")
    
    def _clean_generated_sql(self, sql: str) -> str:
        """Clean up generated SQL"""
        if not sql:
            return ""
        
        # Remove extra whitespace
        sql = ' '.join(sql.split())
        
        # Remove trailing semicolon
        sql = sql.rstrip(';')
        
        # Ensure it starts with SELECT
        if not sql.upper().startswith('SELECT'):
            if 'SELECT' in sql.upper():
                # Extract the SELECT part
                select_pos = sql.upper().find('SELECT')
                sql = sql[select_pos:]
            else:
                return ""
        
        return sql
    
    def _is_valid_sql(self, sql: str) -> bool:
        """Basic validation of generated SQL"""
        if not sql:
            return False
        
        sql_upper = sql.upper()
        
        # Must start with SELECT
        if not sql_upper.startswith('SELECT'):
            return False
        
        # Must contain FROM
        if 'FROM' not in sql_upper:
            return False
        
        # Should not contain dangerous operations
        dangerous = ['DROP', 'DELETE', 'INSERT', 'UPDATE', 'ALTER', 'CREATE', 'TRUNCATE']
        for op in dangerous:
            if op in sql_upper:
                return False
        
        return True