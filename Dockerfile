FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Step 1: Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 2: Copy ALL necessary application files

COPY . /app

# Step 3: Run the application
CMD ["python", "main.py"]
# Set working directory
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .


CMD ["python", "main.py"]
