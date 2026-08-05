import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DeviceContextCard } from '../components/chat/DeviceContextCard';
import type { Conversation } from '../api/types';

describe('DeviceContextCard', () => {
  it('shows empty state when no device context', () => {
    const conv: Conversation = {
      conversationId: 'c1',
      ragflowSessionId: 's1',
      createdAt: '2024-01-01T00:00:00Z',
    };
    render(<DeviceContextCard conversation={conv} />);
    expect(screen.getByText('未绑定设备')).toBeTruthy();
  });

  it('displays equipment ID when present', () => {
    const conv: Conversation = {
      conversationId: 'c1',
      ragflowSessionId: 's1',
      createdAt: '2024-01-01T00:00:00Z',
      equipmentId: 'EQ-1001',
    };
    render(<DeviceContextCard conversation={conv} />);
    expect(screen.getByText('EQ-1001')).toBeTruthy();
  });

  it('displays fixed asset number', () => {
    const conv: Conversation = {
      conversationId: 'c1',
      ragflowSessionId: 's1',
      createdAt: '2024-01-01T00:00:00Z',
      fixedAssetNo: 'FA-2001',
    };
    render(<DeviceContextCard conversation={conv} />);
    expect(screen.getByText(/FA-2001/)).toBeTruthy();
  });

  it('displays fault code', () => {
    const conv: Conversation = {
      conversationId: 'c1',
      ragflowSessionId: 's1',
      createdAt: '2024-01-01T00:00:00Z',
      faultCode: 'E-104',
    };
    render(<DeviceContextCard conversation={conv} />);
    expect(screen.getByText(/E-104/)).toBeTruthy();
  });

  it('shows multiple context tags simultaneously', () => {
    const conv: Conversation = {
      conversationId: 'c1',
      ragflowSessionId: 's1',
      createdAt: '2024-01-01T00:00:00Z',
      equipmentId: 'EQ-1001',
      faultCode: 'E-104',
    };
    render(<DeviceContextCard conversation={conv} />);
    expect(screen.getByText('EQ-1001')).toBeTruthy();
    expect(screen.getByText(/E-104/)).toBeTruthy();
  });

  it('handles null conversation gracefully', () => {
    render(<DeviceContextCard conversation={null} />);
    expect(screen.getByText('未绑定设备')).toBeTruthy();
  });
});
