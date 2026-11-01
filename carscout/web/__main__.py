"""`python -m carscout.web`: serve the UI/API with JSON logs."""
import uvicorn

from carscout.core.config import get_settings
from carscout.core.logging import configure_logging

if __name__ == "__main__":
    s = get_settings()
    configure_logging(s.log_level)
    uvicorn.run("carscout.web.app:app", host="0.0.0.0", port=s.web_port, log_config=None,
                proxy_headers=True)
