#!/bin/sh
for i in {1..100}
do
   echo $i
   curl -H "Content-Type: application/json" -X POST -d '[{"prompt": "What can I do"}]' "http://127.0.0.1:8000/api/v1/default/facebook--opt-125m/run/predict" &
done

