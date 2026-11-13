<?php

namespace Libremapper\Plugin;

use Mcp\Server\ClientGateway;
use Mcp\Server\Handler\ToolHandlerInterface;

class McpToolHandler implements ToolHandlerInterface
{
    public function __construct(private McpTools $tools, private string $name) {}

    public function execute(array $arguments, ClientGateway $gateway): mixed
    {
        return $this->tools->call($this->name, $arguments);
    }
}
