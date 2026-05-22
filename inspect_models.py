import torch
import os

files = ['/mnt/d/student_feedback_system/phobert_multitask/best_model.pt', '/mnt/d/student_feedback_system/phobert_multitask/full_model.pt']

for f in files:
    print(f"--- Inspecting {f} ---")
    if not os.path.exists(f):
        print("File not found")
        continue
    
    checkpoint = torch.load(f, map_location='cpu')
    print(f"Type: {type(checkpoint)}")
    if isinstance(checkpoint, dict):
        print(f"Top-level keys: {list(checkpoint.keys())}")
        
        state_dict = checkpoint.get('model_state_dict', checkpoint if 'phobert.embeddings.word_embeddings.weight' in checkpoint or any('.weight' in k for k in checkpoint.keys()) else None)
        
        if 'model_state_dict' in checkpoint:
            print("state_dict is nested under 'model_state_dict'")
            state_dict = checkpoint['model_state_dict']
        else:
            print("state_dict is likely the top-level object or not found as 'model_state_dict'")
            state_dict = checkpoint

        if isinstance(state_dict, dict):
            # Check for specific weights
            keys_to_check = [
                'phobert.embeddings.word_embeddings.weight',
                'phobert.embeddings.position_embeddings.weight',
                'sentiment_head.weight',
                'topic_head.weight'
            ]
            for head_key in keys_to_check:
                if head_key in state_dict:
                    print(f"{head_key} shape: {state_dict[head_key].shape}")
                else:
                    # Search for similar keys
                    matches = [k for k in state_dict.keys() if head_key.split('.')[-1] in k]
                    print(f"{head_key} not found. Similar keys: {matches[:5]}")
            
            # Identify encoder and heads keys
            encoder_keys = [k for k in state_dict.keys() if 'phobert' in k or 'encoder' in k]
            sentiment_keys = [k for k in state_dict.keys() if 'sentiment' in k]
            topic_keys = [k for k in state_dict.keys() if 'topic' in k]
            
            print(f"Encoder keys (sample): {encoder_keys[:3]}")
            print(f"Sentiment head keys (sample): {sentiment_keys[:3]}")
            print(f"Topic head keys (sample): {topic_keys[:3]}")
    else:
        print("Checkpoint is not a dictionary")
    print("\n")

