# Set up logging
import sys
sys.path.append('.')
import logging

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)
from loguru import logger

from typing import Optional, Dict
from dataclasses import dataclass, field
from pydantic import BaseModel
import os
from contextlib import nullcontext
from transformers.hf_argparser import HfArgumentParser
from transformers.models.auto import AutoConfig, AutoTokenizer, AutoModelForSeq2SeqLM
from fastapi import FastAPI, HTTPException
from uvicorn import run
from sqlite3 import Connection, connect, OperationalError
from seq2seq.utils.pipeline import (Text2SQLGenerationPipeline, Text2SQLGenPipelineWithSchema,
    Text2SQLInput, QuestionWithSchemaInput, get_schema, get_schema_for_display,
    get_db_file_path)
from seq2seq.utils.picard_model_wrapper import PicardArguments, PicardLauncher, with_picard
from seq2seq.utils.dataset import serialize_schema
from seq2seq.utils.dataset import DataTrainingArguments
from seq2seq.utils.spider import spider_get_input
import sqlite3
from pathlib import Path
from typing import List


@dataclass
class BackendArguments:
    """
    Arguments pertaining to model serving.
    """

    model_path: str = field(
        default="tscholak/cxmefzzi",
        metadata={"help": "Path to pretrained model"},
    )
    cache_dir: Optional[str] = field(
        default="/tmp",
        metadata={"help": "Where to cache pretrained models and data"},
    )
    db_path: str = field(
        default="database",
        metadata={"help": "Where to to find the sqlite files"},
    )
    host: str = field(default="0.0.0.0", metadata={"help": "Bind socket to this host"})
    port: int = field(default=8000, metadata={"help": "Bind socket to this port"})
    device: int = field(
        default=0,
        metadata={
            "help": "Device ordinal for CPU/GPU supports. Setting this to -1 will leverage CPU. A non-negative value will run the model on the corresponding CUDA device id."
        },
    )


def main():
    # See all possible arguments by passing the --help flag to this program.
    parser = HfArgumentParser((PicardArguments, BackendArguments, DataTrainingArguments))
    picard_args: PicardArguments
    backend_args: BackendArguments
    data_training_args: DataTrainingArguments
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        picard_args, backend_args, data_training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        picard_args, backend_args, data_training_args = parser.parse_args_into_dataclasses()

    # Initialize config
    logger.info(f'loading model...')
    config = AutoConfig.from_pretrained(
        backend_args.model_path,
        cache_dir=backend_args.cache_dir,
        max_length=data_training_args.max_target_length,
        num_beams=data_training_args.num_beams,
        num_beam_groups=data_training_args.num_beam_groups,
        diversity_penalty=data_training_args.diversity_penalty,
    )

    # Initialize tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        backend_args.model_path,
        cache_dir=backend_args.cache_dir,
        use_fast=True,
    )

    # Initialize Picard if necessary
    with PicardLauncher() if picard_args.launch_picard else nullcontext(None):
        # Get Picard model class wrapper
        if picard_args.use_picard:
            model_cls_wrapper = lambda model_cls: with_picard(
                model_cls=model_cls, picard_args=picard_args, tokenizer=tokenizer
            )
        else:
            model_cls_wrapper = lambda model_cls: model_cls

        # Initialize model
        model = model_cls_wrapper(AutoModelForSeq2SeqLM).from_pretrained(
            backend_args.model_path,
            config=config,
            cache_dir=backend_args.cache_dir,
        )

        # Initalize generation pipeline
        pipe = Text2SQLGenerationPipeline(
            model=model,
            tokenizer=tokenizer,
            db_path=backend_args.db_path,
            prefix=data_training_args.source_prefix,
            normalize_query=data_training_args.normalize_query,
            schema_serialization_type=data_training_args.schema_serialization_type,
            schema_serialization_with_db_id=data_training_args.schema_serialization_with_db_id,
            schema_serialization_with_db_content=data_training_args.schema_serialization_with_db_content,
            device=backend_args.device,
        )

        pipe_with_schema = Text2SQLGenPipelineWithSchema(
            model = model,
            tokenizer = tokenizer,
            db_path = backend_args.db_path,
            normalize_query = data_training_args.normalize_query,
            device = backend_args.device)


        # Initialize REST API
        app = FastAPI()

        class Query(BaseModel):
            question: str
            db_schema: str

        class AskResponse(BaseModel):
            query: str
            execution_results: list
        
        def response(query: str, conn: Connection) -> AskResponse:
            try:
                return AskResponse(query=query, execution_results=conn.execute(query).fetchall())
            except OperationalError as e:
                raise HTTPException(
                    status_code=500, detail=f'while executing "{query}", the following error occurred: {e.args[0]}'
                )

        @app.get("/ask/")
        def ask(db_id: str, question: str):
            try:
                outputs = pipe(
                    inputs=Text2SQLInput(utterance=question, db_id=db_id),
                    num_return_sequences=data_training_args.num_return_sequences
                )
            except OperationalError as e:
                raise HTTPException(status_code=404, detail=e.args[0])
            try:
                conn = connect(backend_args.db_path + "/" + db_id + "/" + db_id + ".sqlite")
                return [response(query=output["generated_text"], conn=conn) for output in outputs]
            finally:
                conn.close()


        @app.post("/ask-with-schema/")
        def ask_with_schema(query: Query):
            try:
                outputs = pipe_with_schema(
                    inputs = QuestionWithSchemaInput(utterance=query.question, schema=query.db_schema),
                    num_return_sequences=data_training_args.num_return_sequences
                )
            except OperationalError as e:
                raise HTTPException(status_code=404, detail=e.args[0])

            return [output["generated_text"] for output in outputs]


        @app.get("/database/")
        def get_database_list():
            db_dir = Path(backend_args.db_path)

            print(f'db_path - {db_dir}')
            db_files = db_dir.rglob("*.sqlite")
            return [db_file.stem for db_file in db_files if db_file.stem == db_file.parent.stem]

        @app.get("/schema/{db_id}")
        def get_schema_for_database(db_id):
            return get_schema(backend_args.db_path, db_id)

        @app.get("/serialized-schema/{db_id}/")
        def get_serialized_schema(db_id, schema_serialization_type = "peteshaw",
                                                schema_serialization_randomized = False,
                                                schema_serialization_with_db_id = True, 
                                                schema_serialization_with_db_content = False
                                               ):
            schema = pipe_with_schema.get_schema_from_cache(db_id)
            serialized_schema = serialize_schema(question='question',
                db_path = backend_args.db_path,
                db_id = db_id,
                db_column_names = schema['db_column_names'],
                db_table_names = schema['db_table_names'],
                schema_serialization_type = schema_serialization_type,
                schema_serialization_randomized = schema_serialization_randomized,
                schema_serialization_with_db_id = schema_serialization_with_db_id,
                schema_serialization_with_db_content = schema_serialization_with_db_content, 
                include_foreign_keys=data_training_args.include_foreign_keys_in_schema,
                foreign_keys=schema['db_foreign_keys']
                )
            return spider_get_input('question', serialized_schema, prefix='')


        @app.post("/schema/{db_id}")
        def create_schema(db_id, queries: List[str]):
            db_file_path = Path(get_db_file_path(backend_args.db_path, db_id))

            if db_file_path.exists():
                raise HTTPException(status_code=409, detail="database already exists")

            # create parent directory if it doesn't exist
            db_file_path.parent.mkdir(parents=True, exist_ok=True)

            print(f'creating database {db_file_path.as_posix()}...')
            
            con = sqlite3.connect(db_file_path.as_posix())
            cur = con.cursor()
            try:
                for query in queries:
                    cur.execute(query)
                con.commit()
            except OperationalError as e:
                raise HTTPException(status_code=400, detail=e.args[0])
            finally:
                con.close()
            
            return get_schema(backend_args.db_path, db_id)

        @app.patch("/schema/{db_id}")
        def update_schema(db_id, queries: List[str]):
            db_file_path = Path(get_db_file_path(backend_args.db_path, db_id))

            if not db_file_path.exists():
                raise HTTPException(status_code=404, detail="database not found")

            print(f'updating database {db_file_path.as_posix()}...')
            
            con = sqlite3.connect(db_file_path.as_posix())
            cur = con.cursor()
            try:
                for query in queries:
                    cur.execute(query)
                con.commit()
            except OperationalError as e:
                raise HTTPException(status_code=400, detail=e.args[0])
            finally:
                con.close()
            
            return get_schema(backend_args.db_path, db_id)

        

        # Run app
        run(app=app, host=backend_args.host, port=backend_args.port)


if __name__ == "__main__":
    print('serving....')
    main()
