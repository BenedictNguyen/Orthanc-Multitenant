# Mock SKG API server for testing purposes
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/api/v1/new-study", methods=["POST"])
def new_study():
    data = request.json
    print("[MOCK SKG] Received New Study:")
    print(json.dumps(data, indent=2))
    return jsonify({"message": "New study received", "data": data}), 200

@app.route("/api/v1/stable-study", methods=["POST"])
def stable_study():
    data = request.json
    print("[MOCK SKG] Received Stable Study:")
    print(json.dumps(data, indent=2))
    return jsonify({"message": "Stable study received", "data": data}), 200

@app.route("/api/v1/health", methods=["GET"])
def health_check():
    return {"status": "ok", "service": "RIS Flask API"}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
