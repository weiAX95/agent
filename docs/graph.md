                           用户 message
                                │
                                ▼
                          chat()
                                │
                    ┌───────────┴───────────┐
                    │                       │
              【Memory Read】          【Memory Write】
               回答前召回                回答后判断
                    │                       │
                    ▼                       │
        should_recall_memory()              │
                    │                       │
          ┌─────────┴─────────┐             │
          │                   │             │
       False                 True           │
          │                   │             │
          │                   ▼             │
          │          route_memory_keys()    │
          │             Rule Router         │
          │                   │             │
          │        ┌──────────┴─────────┐   │
          │        │                    │   │
          │      命中                  未命中 │
          │        │                    │   │
          │        ▼                    ▼   │
          │  get_memories()   route_memory_keys_with_llm()
          │        │                 LLM Router
          │        │                    │
          │        │          ┌─────────┴─────────┐
          │        │          │                   │
          │        │        命中                 未命中
          │        │          │                   │
          │        │          ▼                   ▼
          │        │    get_memories()    _recall_memories()
          │        │          │             Vector Search
          │        │          │                   │
          │        └──────────┴───────────┬───────┘
          │                               │
          │                               ▼
          │                        memory_context
          │                               │
          └───────────────────────┬───────┘
                                  ▼
                             agent.invoke()
                                  │
                                  ▼
                              最终回答
                                  │
                                  ▼
                           judge_memory()
                                  │
                            Candidate LLM
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
              should_save=False          should_save=True
                    │                           │
                    │                           ▼
                    │                 get_memories([key])
                    │                           │
                    │                     memory_cache
                    │                     优先复用旧查询
                    │                           │
                    │                           ▼
                    │                  resolve_memories()
                    │                 Batch Resolution LLM
                    │                           │
                    │              ┌────────────┼────────────┐
                    │              │            │            │
                    │            none        insert        replace
                    │              │            │            │
                    │              │            ▼            ▼
                    │              │      save_to_memory  replace_memory
                    │              │            │            │
                    │              │            └──────┬─────┘
                    │              │                   ▼
                    │              │         Milvus insert/upsert
                    │              │
                    └──────────────┴──────────────────────────
