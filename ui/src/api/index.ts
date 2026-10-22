import { createLiveApi } from './live';
import { createMockApi } from './mock';
import type { NexusApi } from './types';

export const api: NexusApi = import.meta.env.MODE === 'mock' ? createMockApi() : createLiveApi();
