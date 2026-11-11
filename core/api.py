import os
import meraki
from .logger import logger

class MerakiSession:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MerakiSession, cls).__new__(cls)
            cls._instance.api = None
        return cls._instance

    def get_dashboard(self):
        if self.api is None:
            # Strictly pull from OS environment variable
            api_key = os.getenv("MK_CSM_KEY")
            
            if not api_key:
                logger.error("Environment variable 'MK_CSM_KEY' not found.")
                raise EnvironmentError("Missing MK_CSM_KEY environment variable.")
            
            self.api = meraki.DashboardAPI(
                api_key=api_key,
                suppress_logging=True,
                print_console=False
            )
        return self.api

session = MerakiSession()