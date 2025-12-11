class Logger:
    _buffer = []

    @classmethod
    def get(cls):
        """Return Logger class (singleton style)"""
        return cls

    @classmethod
    def info(cls, msg: str):
        """Log an info message"""
        line = f"[INFO] {msg}"
        cls._buffer.append(line)
        print(line)  # Prints to Blender console

    @classmethod
    def error(cls, msg: str):
        """Log an error message"""
        line = f"[ERROR] {msg}"
        cls._buffer.append(line)
        print(line)  # Prints to Blender console

    @classmethod
    def tail(cls, n=50):
        """Return last n log entries"""
        return cls._buffer[-n:]
