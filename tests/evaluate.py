import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Dict, List
from unittest.mock import patch

from api.models import PipelineFailureEvent
from agents.coordinator import run_diagnosis_workflow
from datetime import datetime, timezone

def print_table(headers: List[str], rows: List[List[str]]):
    """Prints a formatted markdown-style table."""
    col_widths = [max(len(str(item)) for item in col) for col in zip(headers, *rows)]
    
    row_format = " | ".join(["{:<" + str(width) + "}" for width in col_widths])
    print(row_format.format(*headers))
    print("-|-".join(["-" * width for width in col_widths]))
    
    for row in rows:
        print(row_format.format(*row))

async def evaluate():
    fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    if not os.path.exists(fixtures_dir):
        print(f"Error: Fixtures directory not found at {fixtures_dir}")
        return

    fixture_files = [f for f in os.listdir(fixtures_dir) if f.endswith('.json')]
    if not fixture_files:
        print("No fixtures found. Run generate_fixtures.py first.")
        return

    print(f"Loading {len(fixture_files)} historical failure logs for evaluation...\n")

    total_logs = len(fixture_files)
    correct_predictions = 0
    total_confidence = 0.0
    total_processing_time = 0.0
    
    # metrics[category] = {"true_pos": 0, "false_pos": 0, "false_neg": 0}
    metrics = defaultdict(lambda: {"true_pos": 0, "false_pos": 0, "false_neg": 0})

    for i, file_name in enumerate(fixture_files, 1):
        file_path = os.path.join(fixtures_dir, file_name)
        with open(file_path, "r") as f:
            data = json.load(f)
            
        expected_category = data["expected_category"]
        log_content = data["log"]
        
        # Create a dummy event
        event = PipelineFailureEvent(
            run_id=i,
            repo_full_name="eval/repo",
            branch="main",
            commit_sha="abcdef",
            workflow_name="Eval",
            failed_at=datetime.now(timezone.utc)
        )
        
        # Mock fetch_github_logs to return our fixture log
        with patch("agents.coordinator.fetch_github_logs", return_value={"truncated_logs": log_content, "error_message": "", "stack_trace": "", "failed_step": "eval_step"}):
            # We also mock get_cloud_context to be empty to focus purely on log analysis
            with patch("agents.coordinator.get_cloud_context", return_value={"infrastructure_summary": "No cloud context available."}):
                print(f"[{i}/{total_logs}] Evaluating {file_name} (Expected: {expected_category})... ", end="", flush=True)
                
                start_time = time.time()
                try:
                    result = await run_diagnosis_workflow(event)
                    predicted_category = result["diagnosis"]["root_cause_category"]
                    confidence = result["diagnosis"]["confidence"]
                except Exception as e:
                    predicted_category = "error"
                    confidence = 0.0
                    print(f"Failed ({str(e)})")
                    continue
                
                elapsed = time.time() - start_time
                total_processing_time += elapsed
                total_confidence += confidence
                
                print(f"Predicted: {predicted_category} | Confidence: {confidence:.2f} | Time: {elapsed:.2f}s")
                
                if predicted_category == expected_category:
                    correct_predictions += 1
                    metrics[expected_category]["true_pos"] += 1
                else:
                    metrics[expected_category]["false_neg"] += 1
                    metrics[predicted_category]["false_pos"] += 1

    # Calculate overall metrics
    overall_accuracy = correct_predictions / total_logs if total_logs > 0 else 0
    avg_confidence = total_confidence / total_logs if total_logs > 0 else 0
    avg_processing_time = total_processing_time / total_logs if total_logs > 0 else 0
    
    # Calculate False Positive Rate = FP / (FP + TN)
    # Total Negatives for a category C = total_logs - actual positives of C
    # FPR = False Positives of C / Total Negatives of C
    
    print("\n" + "="*50)
    print(" 📊 PERFORMANCE EVALUATION RESULTS ")
    print("="*50 + "\n")
    
    print(f"Overall Accuracy      : {overall_accuracy:.2%}")
    print(f"Average Confidence    : {avg_confidence:.2f}")
    print(f"Average Time/Log      : {avg_processing_time:.2f}s\n")
    
    # Calculate Per-Category Metrics
    headers = ["Category", "Precision", "Recall", "FPR", "True Pos", "False Pos", "False Neg"]
    rows = []
    
    for category, counts in sorted(metrics.items()):
        tp = counts["true_pos"]
        fp = counts["false_pos"]
        fn = counts["false_neg"]
        
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0
        
        actual_positives = tp + fn
        total_negatives = total_logs - actual_positives
        fpr = fp / total_negatives if total_negatives > 0 else 0
        
        rows.append([
            category,
            f"{precision:.2%}",
            f"{recall:.2%}",
            f"{fpr:.2%}",
            str(tp),
            str(fp),
            str(fn)
        ])
        
    print_table(headers, rows)
    print("\nEvaluation complete.")

if __name__ == "__main__":
    asyncio.run(evaluate())
