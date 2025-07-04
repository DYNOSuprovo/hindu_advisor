# fastapi_app.py

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
import logging
import os # Import os for environment variables
from config import GROQ_API_KEY # Assuming GEMINI_API_KEY will be from os.getenv
from llms import init_gemini_llm, init_embeddings
from vector_db import load_chroma_db
# Import get_session_history from qa_chain, not store
from qa_chain import create_conversational_chain, get_session_history, merge_groq_and_rag_answers, GoogleSheetChatMessageHistory # Import GoogleSheetChatMessageHistory
from groq_api import cached_groq_answers
from utils import extract_spiritual_concept, extract_life_problem, extract_scripture_source, clean_response_text, clean_suggestions, contains_table_request
import uuid
import re # Keep for clean_response_text if it was used directly, but now imported
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Enable CORS for Flutter app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    query: str
    session_id: str = None
    format_table: bool = False

# Retrieve API keys and Google Sheet credentials from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not GEMINI_API_KEY:
    logging.error("GEMINI_API_KEY environment variable not set.")
if not GROQ_API_KEY: # This is still from config.py based on original, can move to env as well
    logging.warning("GROQ_API_KEY environment variable not set. Groq suggestions will be skipped.")
if not GOOGLE_CREDENTIALS_JSON:
    logging.error("GOOGLE_CREDENTIALS_JSON environment variable not set. Chat history will not be saved.")


@app.post("/chat")
async def handle_chat(request: ChatRequest):
    session_id = request.session_id or f"session_{uuid.uuid4().hex}"
    effective_query = request.query
    
    format_as_table = contains_table_request(effective_query)
    logging.info(f"Session {session_id}: Query '{effective_query}' received. Table format requested: {format_as_table}")

    try:
        # Initialize components
        llm = init_gemini_llm(GEMINI_API_KEY)
        embedding = init_embeddings(GEMINI_API_KEY) # Pass API key to init_embeddings
        db = load_chroma_db(embedding)
        retriever = db.as_retriever(search_kwargs={"k": 5})
        
        # Create conversational chain, passing Google Sheet credentials
        conversational_qa_chain = create_conversational_chain(llm, retriever, GOOGLE_CREDENTIALS_JSON)
        
        # Extract metadata from query
        spiritual_concept = extract_spiritual_concept(effective_query)
        life_problem = extract_life_problem(effective_query)
        scripture_source = extract_scripture_source(effective_query)

        # Get RAG result
        logging.info(f"Session {session_id}: Invoking RAG chain for query: '{effective_query}'")
        rag_result = conversational_qa_chain.invoke({
            "question": effective_query,
            "spiritual_concept": spiritual_concept,
            "life_problem": life_problem,
            "scripture_source": scripture_source
        }, config={"configurable": {"session_id": session_id}})
        
        raw_rag_answer = rag_result.get("answer", "Could not retrieve from knowledge base.")
        rag_answer = clean_response_text(raw_rag_answer)
        logging.info(f"Session {session_id}: RAG raw answer length: {len(raw_rag_answer)}")
        
        # Get suggestions from other models
        suggestions = {"llama": "N/A", "mixtral": "N/A", "gemma": "N/A"}
        if GROQ_API_KEY:
            logging.info(f"Session {session_id}: Fetching Groq suggestions.")
            raw_suggestions = cached_groq_answers(
                effective_query,
                GROQ_API_KEY,
                spiritual_concept,
                life_problem,
                scripture_source
            )
            suggestions = clean_suggestions(raw_suggestions)
            logging.info(f"Session {session_id}: Groq suggestions fetched.")
        else:
            logging.warning(f"Session {session_id}: GROQ_API_KEY not set. Skipping Groq suggestions.")

        # Merge RAG answer and Groq suggestions
        final_answer = await merge_groq_and_rag_answers(
            llm,
            rag_answer,
            suggestions,
            spiritual_concept,
            life_problem,
            scripture_source,
            format_as_table
        )
        final_answer = clean_response_text(final_answer)
        
        return {
            "answer": final_answer,
            "suggestions": suggestions,
            "session_id": session_id
        }
        
    except Exception as e:
        logging.error(f"API Error for session {session_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "message": "Hindu Scripture Advisor API is running"}

@app.get("/sessions/{session_id}/history")
async def get_chat_history_endpoint(session_id: str): # Renamed to avoid conflict with imported function
    """Get chat history for a session"""
    try:
        # Instantiate history object with credentials
        history = GoogleSheetChatMessageHistory(session_id, GOOGLE_CREDENTIALS_JSON)
        messages = []
        for message in history.messages:
            content = clean_response_text(message.content) if hasattr(message, 'content') else str(message)
            messages.append({
                "type": message.type,
                "content": content
            })
        logging.info(f"Retrieved {len(messages)} history entries for session {session_id}.")
        return {
            "session_id": session_id,
            "messages": messages,
            "count": len(messages)
        }
    except Exception as e:
        logging.error(f"Error retrieving history for session {session_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not retrieve chat history")

@app.delete("/sessions/{session_id}")
async def clear_session(session_id: str):
    """Clear chat history for a session"""
    try:
        # Instantiate history object and clear
        history = GoogleSheetChatMessageHistory(session_id, GOOGLE_CREDENTIALS_JSON)
        history.clear()
        logging.info(f"Cleared session {session_id}")
        return {"message": f"Session {session_id} cleared successfully"}
    except Exception as e:
        logging.error(f"Error clearing session {session_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not clear session: {str(e)}")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

if __name__ == "__main__":
    import uvicorn
    # When deploying to Render, the port will be provided by the environment,
    # so listen on 0.0.0.0 and use the PORT environment variable if available.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
