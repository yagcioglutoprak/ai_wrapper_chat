#!/usr/bin/env python3
"""Extract OPUS MODIFIED sections with their responses from proxy_requests.log"""

import sys

def extract_modified_sections(log_path="/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log", count=4):
    with open(log_path, 'r') as f:
        content = f.read()
    
    # Split by request separator
    blocks = content.split('=' * 80)
    
    results = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if 'REQUEST (OPUS MODIFIED)' in block:
            entry = {'request': block.strip(), 'responses': []}
            
            # Look at the next 4 blocks for Response Status
            for j in range(1, 5):
                if i + j < len(blocks):
                    next_block = blocks[i + j].strip()
                    if next_block.startswith('Response Status:'):
                        entry['responses'].append(next_block)
            
            results.append(entry)
        i += 1
    
    # Get last N entries
    results = results[-count:]
    
    output = []
    for idx, entry in enumerate(results, 1):
        output.append(f"\n{'#' * 80}")
        output.append(f"# MODIFIED REQUEST {idx}")
        output.append('#' * 80)
        output.append(entry['request'])  # FULL request, no truncation
        
        for resp_idx, resp in enumerate(entry['responses'], 1):
            output.append(f"\n--- RESPONSE {resp_idx} ---")
            output.append(resp)  # FULL response, no truncation
    
    return '\n'.join(output)

if __name__ == "__main__":
    log_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    output_file = "/Users/toprakyagcioglu/.rovodev/modified_requests.txt"
    
    result = extract_modified_sections(log_path, count)
    
    with open(output_file, 'w') as f:
        f.write(result)
    
    print(f"Saved full output to {output_file}")
