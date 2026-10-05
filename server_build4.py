import server
import build4_support

# Install Build 4 routes only after the main FastAPI app and dependencies are loaded.
build4_support.install_build4_routes(server.app)

app = server.app
