import server
import build4_support
import build5_support

# Install Build 4 routes only after the main FastAPI app and dependencies are loaded.
build4_support.install_build4_routes(server.app)

# Install Build 5 safety policy route and initialize moderation tables.
build5_support._init_build5_tables()
build5_support.install_build5_routes(server.app)

app = server.app
